# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "psycopg2-binary>=2.9.0",
#     "python-dotenv>=1.0.0",
# ]
# ///

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote_plus, urlsplit

from dotenv import find_dotenv, load_dotenv

from sql_audit.application import AuditReport, convert_legacy_report_to_audit_report
from sql_audit.infrastructure.queries import (
    SQL_DEAD_TUPLES_ALL_PSYCOPG,
    SQL_DEAD_TUPLES_SCHEMAS_PSYCOPG,
    SQL_HOT_PSYCOPG,
    SQL_INVALID_INDEXES_ALL_PSYCOPG,
    SQL_INVALID_INDEXES_SCHEMAS_PSYCOPG,
    SQL_LOW_USAGE_PSYCOPG,
    SQL_REDUNDANT_PSYCOPG,
    SQL_UNINDEXED_FKS_ALL_PSYCOPG,
    SQL_UNINDEXED_FKS_SCHEMAS_PSYCOPG,
)

try:
    import psycopg2
    import psycopg2.extras

    HAS_PSYCOPG2 = True
except ImportError:  # pragma: no cover
    HAS_PSYCOPG2 = False


ALL_CHECKS = ["redundant", "hot", "low-usage", "invalid", "unindexed-fks", "dead-tuples"]
CHECK_FIELDS: dict[str, str] = {
    "redundant":     "redundant_indexes",
    "hot":           "hot_issues",
    "low-usage":     "low_usage_indexes",
    "invalid":       "invalid_indexes",
    "unindexed-fks": "unindexed_fks",
    "dead-tuples":   "autovacuum_dead_tuples",
}
DEFAULT_CONNECT_TIMEOUT = 10


@dataclass(frozen=True)
class HotUpdateIssue:
    table_name: str
    total_updates: int
    hot_updates: int
    hot_ratio_pct: float
    fillfactor: int
    fillfactor_warning: bool
    table_rows: int
    table_size: str


@dataclass(frozen=True)
class RedundantIndexIssue:
    table_name: str
    redundant_index: str
    redundant_size: str
    covering_index: str
    redundant_def: str
    covering_def: str


@dataclass(frozen=True)
class LowUsageIndexIssue:
    table_name: str
    index_name: str
    size: str
    index_scans: int
    table_writes: int
    read_write_ratio: float
    table_rows: int
    table_size: str


@dataclass(frozen=True)
class InvalidIndexIssue:
    child_table: str
    invalid_index: str
    index_size: str


@dataclass(frozen=True)
class UnindexedFKIssue:
    child_table: str
    fk_name: str
    parent_table: str
    fk_definition: str


@dataclass(frozen=True)
class DeadTuplesIssue:
    table_name: str
    dead_tuples: int
    live_tuples: int
    dead_tuple_pct: float
    last_autovacuum: datetime | None = None
    last_vacuum: datetime | None = None


@dataclass(frozen=True)
class DatabaseHealthReport:
    hot_issues: list[HotUpdateIssue] = field(default_factory=list)
    redundant_indexes: list[RedundantIndexIssue] = field(default_factory=list)
    low_usage_indexes: list[LowUsageIndexIssue] = field(default_factory=list)
    invalid_indexes: list[InvalidIndexIssue] = field(default_factory=list)
    unindexed_fks: list[UnindexedFKIssue] = field(default_factory=list)
    autovacuum_dead_tuples: list[DeadTuplesIssue] = field(default_factory=list)

    @property
    def has_critical_issues(self) -> bool:
        return bool(self.invalid_indexes or self.unindexed_fks or self.autovacuum_dead_tuples)

    def to_audit_report(
        self,
        database: str = "localhost",
        checks: list[str] | None = None,
        observed_at: datetime | None = None,
        server_version: str = "unknown",
        duration_ms: float = 0.0,
        schemas: list[str] | None = None,
        min_size_bytes: int = 0,
        min_table_rows: int = 10000,
        audit_id: str | None = None,
    ) -> AuditReport:
        return convert_legacy_report_to_audit_report(
            report=self,
            database=database,
            checks=checks or list(ALL_CHECKS),
            observed_at=observed_at,
            server_version=server_version,
            duration_ms=duration_ms,
            schemas=schemas,
            min_size_bytes=min_size_bytes,
            min_table_rows=min_table_rows,
            audit_id=audit_id,
        )


class DatabaseConnectionError(Exception):
    """Raised when the database connection cannot be established."""


class PostgresHealthAuditor:
    """Audits PostgreSQL catalog and runtime statistics for indexing and storage anti-patterns."""

    def __init__(self, db_url: str, connect_timeout: int = DEFAULT_CONNECT_TIMEOUT):
        self.db_url = db_url
        self.connect_timeout = connect_timeout

    def run_audit(
        self,
        min_hot_ratio_pct: float = 30.0,
        min_updates_threshold: int = 50,
        max_rw_ratio: float = 0.05,
        checks: list[str] | None = None,
        schemas: list[str] | None = None,
        min_size_bytes: int = 0,
        min_table_rows: int = 10000,
    ) -> DatabaseHealthReport:
        if not HAS_PSYCOPG2:
            raise RuntimeError("psycopg2 is not installed. Run: uv add psycopg2-binary")

        try:
            conn = psycopg2.connect(self.db_url, connect_timeout=self.connect_timeout)
        except Exception as exc:  # noqa: BLE001
            raise DatabaseConnectionError(str(exc)) from exc

        selected = checks if checks is not None else ALL_CHECKS

        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                hot_issues: list[HotUpdateIssue] = []
                redundant_indexes: list[RedundantIndexIssue] = []
                low_usage: list[LowUsageIndexIssue] = []
                invalid_indexes: list[InvalidIndexIssue] = []
                unindexed_fks: list[UnindexedFKIssue] = []
                autovacuum_dead_tuples: list[DeadTuplesIssue] = []

                if "hot" in selected:
                    hot_issues = self._audit_hot_and_fillfactor(
                        cur, min_hot_ratio_pct, min_updates_threshold, schemas, min_table_rows
                    )
                if "redundant" in selected:
                    redundant_indexes = self._audit_redundant_indexes(cur, schemas, min_size_bytes)
                if "low-usage" in selected:
                    low_usage = self._audit_low_usage_indexes(
                        cur, max_rw_ratio, schemas, min_size_bytes, min_table_rows
                    )
                if "invalid" in selected:
                    invalid_indexes = self._audit_invalid_indexes(cur, schemas)
                if "unindexed-fks" in selected:
                    unindexed_fks = self._audit_unindexed_fks(cur, schemas)
                if "dead-tuples" in selected:
                    autovacuum_dead_tuples = self._audit_autovacuum_dead_tuples(cur, schemas)

                return DatabaseHealthReport(
                    hot_issues=hot_issues,
                    redundant_indexes=redundant_indexes,
                    low_usage_indexes=low_usage,
                    invalid_indexes=invalid_indexes,
                    unindexed_fks=unindexed_fks,
                    autovacuum_dead_tuples=autovacuum_dead_tuples,
                )
        finally:
            conn.close()

    def _audit_hot_and_fillfactor(
        self,
        cur: Any,
        min_ratio: float,
        min_updates: int,
        schemas: list[str] | None,
        min_table_rows: int,
    ) -> list[HotUpdateIssue]:
        query = SQL_HOT_PSYCOPG
        params: list[Any] = [min_updates, min_ratio, min_table_rows]
        if schemas:
            query += "          AND ns.nspname = ANY(%s)\n"
            params.append(schemas)
        query += "        ORDER BY t.n_tup_upd DESC;"
        cur.execute(query, params)
        rows = cur.fetchall()
        issues: list[HotUpdateIssue] = []
        for r in rows:
            data: dict[str, Any] = dict(r)
            data["fillfactor_warning"] = data["fillfactor"] == 100
            issues.append(HotUpdateIssue(**data))
        return issues

    def _audit_redundant_indexes(
        self, cur: Any, schemas: list[str] | None, min_size_bytes: int
    ) -> list[RedundantIndexIssue]:
        query = SQL_REDUNDANT_PSYCOPG
        params: list[Any] = [min_size_bytes]
        if schemas:
            query += "          AND n.nspname = ANY(%s)\n"
            params.append(schemas)
        query += "        ORDER BY p1.size_bytes DESC;"
        cur.execute(query, params)
        rows = cur.fetchall()
        issues: list[RedundantIndexIssue] = []
        for r in rows:
            data: dict[str, Any] = dict(r)
            issues.append(RedundantIndexIssue(**data))
        return issues

    def _audit_low_usage_indexes(
        self,
        cur: Any,
        max_ratio: float,
        schemas: list[str] | None,
        min_size_bytes: int,
        min_table_rows: int,
    ) -> list[LowUsageIndexIssue]:
        query = SQL_LOW_USAGE_PSYCOPG
        params: list[Any] = [max_ratio, min_size_bytes, min_table_rows]
        if schemas:
            query += "          AND ns.nspname = ANY(%s)\n"
            params.append(schemas)
        query += "        ORDER BY pg_relation_size(i.indexrelid) DESC;"
        cur.execute(query, params)
        rows = cur.fetchall()
        issues: list[LowUsageIndexIssue] = []
        for r in rows:
            data: dict[str, Any] = dict(r)
            issues.append(LowUsageIndexIssue(**data))
        return issues

    def _audit_invalid_indexes(
        self, cur: Any, schemas: list[str] | None
    ) -> list[InvalidIndexIssue]:
        """Returns indexes with indisvalid = FALSE — these need immediate attention."""
        if schemas:
            cur.execute(SQL_INVALID_INDEXES_SCHEMAS_PSYCOPG, [schemas])
        else:
            cur.execute(SQL_INVALID_INDEXES_ALL_PSYCOPG)
        rows = cur.fetchall()
        return [InvalidIndexIssue(**dict(r)) for r in rows]

    def _audit_unindexed_fks(
        self, cur: Any, schemas: list[str] | None
    ) -> list[UnindexedFKIssue]:
        """Returns foreign keys that lack a supporting index on the referencing column(s)."""
        if schemas:
            cur.execute(SQL_UNINDEXED_FKS_SCHEMAS_PSYCOPG, [schemas])
        else:
            cur.execute(SQL_UNINDEXED_FKS_ALL_PSYCOPG)
        rows = cur.fetchall()
        return [UnindexedFKIssue(**dict(r)) for r in rows]

    def _audit_autovacuum_dead_tuples(
        self, cur: Any, schemas: list[str] | None
    ) -> list[DeadTuplesIssue]:
        """Returns tables with high dead-tuple ratios indicating autovacuum lag."""
        if schemas:
            cur.execute(SQL_DEAD_TUPLES_SCHEMAS_PSYCOPG, [schemas])
        else:
            cur.execute(SQL_DEAD_TUPLES_ALL_PSYCOPG)
        rows = cur.fetchall()
        return [DeadTuplesIssue(**dict(r)) for r in rows]


def render_text(report: DatabaseHealthReport) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 65)
    lines.append("        POSTGRESQL STORAGE & INDEX HEALTH REPORT")
    lines.append("=" * 65)

    # 1. Invalid Indexes (Critical)
    lines.append("")
    lines.append(f"[1] INVALID INDEXES (indisvalid = false): {len(report.invalid_indexes)}")
    if not report.invalid_indexes:
        lines.append("  -> OK: No invalid indexes detected.")
    else:
        for inv in report.invalid_indexes:
            lines.append(f"  * Table: {inv.child_table}")
            lines.append(f"    - Invalid index: {inv.invalid_index} (Size: {inv.index_size})")
            lines.append(
                f"    - Suggestion: DROP INDEX CONCURRENTLY {inv.invalid_index}; "
                "then rebuild if necessary."
            )
            lines.append("")

    # 2. Unindexed Foreign Keys (Critical)
    lines.append(f"[2] UNINDEXED FOREIGN KEYS: {len(report.unindexed_fks)}")
    if not report.unindexed_fks:
        lines.append("  -> OK: No unindexed foreign keys detected.")
    else:
        for fk in report.unindexed_fks:
            lines.append(f"  * Table: {fk.child_table} -> {fk.parent_table}")
            lines.append(f"    - FK: {fk.fk_name}")
            lines.append(f"    - Definition: {fk.fk_definition}")
            lines.append(
                "    - Suggestion: Consider CREATE INDEX CONCURRENTLY to prevent table locks."
            )
            lines.append("")

    # 3. Autovacuum & Dead Tuples Lag (Critical)
    lines.append(f"[3] AUTOVACUUM & DEAD TUPLES LAG: {len(report.autovacuum_dead_tuples)}")
    if not report.autovacuum_dead_tuples:
        lines.append("  -> OK: No tables with autovacuum lag or excessive dead tuples.")
    else:
        for dt in report.autovacuum_dead_tuples:
            lines.append(f"  * Table: {dt.table_name}")
            lines.append(
                f"    - Dead Tuples: {dt.dead_tuples:,} ({dt.dead_tuple_pct}%) | "
                f"Live Tuples: {dt.live_tuples:,}"
            )
            last_auto = dt.last_autovacuum.isoformat() if dt.last_autovacuum else "never"
            last_vac = dt.last_vacuum.isoformat() if dt.last_vacuum else "never"
            lines.append(f"    - Last Autovacuum: {last_auto} | Last Vacuum: {last_vac}")
            lines.append(
                "    - Suggestion: Check long-running transactions and autovacuum settings."
            )
            lines.append("")

    # 4. Redundant Indexes
    lines.append(f"[4] REDUNDANT / PREFIX INDEXES FOUND: {len(report.redundant_indexes)}")
    if not report.redundant_indexes:
        lines.append("  -> OK: No redundant indexes detected.")
    else:
        for idx in report.redundant_indexes:
            lines.append(f"  * Table: {idx.table_name}")
            lines.append(f"    - Redundant: {idx.redundant_index} (Size: {idx.redundant_size})")
            lines.append(f"    - Covered by: {idx.covering_index}")
            lines.append(f"    - Redundant def: {idx.redundant_def}")
            lines.append(f"    - Suggestion: Consider DROP INDEX {idx.redundant_index};")
            lines.append("")

    # 5. HOT Updates & Fillfactor
    lines.append(f"[5] LOW HOT UPDATE EFFICIENCY: {len(report.hot_issues)}")
    if not report.hot_issues:
        lines.append("  -> OK: No tables with low HOT update ratio.")
    else:
        for issue in report.hot_issues:
            warning = (
                " [!] WARNING: Default 100% fillfactor prevents HOT"
                if issue.fillfactor_warning
                else ""
            )
            lines.append(f"  * Table: {issue.table_name}")
            lines.append(
                f"    - HOT Ratio: {issue.hot_ratio_pct}% "
                f"({issue.hot_updates} HOT / {issue.total_updates} total updates)"
            )
            lines.append(
                f"    - Rows: {issue.table_rows} | Table size: {issue.table_size}"
            )
            lines.append(f"    - Fillfactor: {issue.fillfactor}%{warning}")
            if issue.fillfactor_warning:
                lines.append(
                    f"    - Suggestion: ALTER TABLE {issue.table_name} "
                    f"SET (fillfactor = 85); VACUUM FULL {issue.table_name};"
                )
                lines.append("")

    # 6. Low Usage Indexes
    lines.append(
        f"[6] UNPROFITABLE / HIGH-WRITE LOW-READ INDEXES: {len(report.low_usage_indexes)}"
    )
    if not report.low_usage_indexes:
        lines.append("  -> OK: No unprofitable indexes detected.")
    else:
        for low in report.low_usage_indexes:
            lines.append(
                f"  * Table: {low.table_name} (Rows: {low.table_rows} | Size: {low.table_size})"
            )
            lines.append(f"    - Index: {low.index_name} (Size: {low.size})")
            lines.append(
                f"    - Read Scans: {low.index_scans} | "
                f"Table Writes: {low.table_writes} (Ratio: {low.read_write_ratio})"
            )
            lines.append("    - Suggestion: Evaluate if this index is required for queries.")
            lines.append("")

    lines.append("=" * 65)
    lines.append("")
    return "\n".join(lines)


def print_report(report: DatabaseHealthReport) -> None:
    print(render_text(report))


def render_quiet(report: DatabaseHealthReport) -> str:
    lines = [
        f"invalid_indexes: {len(report.invalid_indexes)}",
        f"unindexed_fks: {len(report.unindexed_fks)}",
        f"autovacuum_dead_tuples: {len(report.autovacuum_dead_tuples)}",
        f"redundant_indexes: {len(report.redundant_indexes)}",
        f"hot_issues: {len(report.hot_issues)}",
        f"low_usage_indexes: {len(report.low_usage_indexes)}",
    ]
    return "\n".join(lines)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def render_json(
    report: DatabaseHealthReport, database: str, checks: list[str]
) -> dict[str, Any]:
    issues: dict[str, list[dict[str, Any]]] = {}
    for check in checks:
        field_name = CHECK_FIELDS[check]
        issues[check] = [
            {key: _jsonable(value) for key, value in asdict(issue).items()}
            for issue in getattr(report, field_name)
        ]
    return {
        "database": database,
        "checks": list(checks),
        "has_critical_issues": report.has_critical_issues,
        "summary": {
            "invalid_indexes": len(report.invalid_indexes),
            "unindexed_fks": len(report.unindexed_fks),
            "autovacuum_dead_tuples": len(report.autovacuum_dead_tuples),
            "redundant_indexes": len(report.redundant_indexes),
            "hot_issues": len(report.hot_issues),
            "low_usage_indexes": len(report.low_usage_indexes),
        },
        "issues": issues,
    }


def has_issues(report: DatabaseHealthReport) -> bool:
    return bool(
        report.hot_issues
        or report.redundant_indexes
        or report.low_usage_indexes
        or report.invalid_indexes
        or report.unindexed_fks
        or report.autovacuum_dead_tuples
    )


def redact_db_url(db_url: str) -> str:
    """Return only the host portion of a connection URL (never credentials)."""
    try:
        parts = urlsplit(db_url)
        host = parts.hostname
        return host if host else "localhost"
    except ValueError:
        return "localhost"


def _build_libpq_url() -> str | None:
    host = os.getenv("PGHOST")
    dbname = os.getenv("PGDATABASE")
    user = os.getenv("PGUSER")
    if not any((host, dbname, user)):
        return None

    port = os.getenv("PGPORT") or "5432"
    password = os.getenv("PGPASSWORD") or ""
    host = host or "localhost"
    user = user or ""
    dbname = dbname or ""

    auth = ""
    if user:
        auth = quote_plus(user)
        if password:
            auth += f":{quote_plus(password)}"
        auth += "@"
    path = f"/{quote_plus(dbname)}" if dbname else ""
    return f"postgresql://{auth}{host}:{port}{path}"


def _build_postgres_env_url() -> str | None:
    """Fallback for docker-compose style POSTGRES_* variables (e.g. spinned from .env)."""
    dbname = os.getenv("POSTGRES_DB")
    if not dbname:
        return None

    user = quote_plus(os.getenv("POSTGRES_USER") or "")
    password = quote_plus(os.getenv("POSTGRES_PASSWORD") or "")
    host = os.getenv("POSTGRES_HOST") or "localhost"
    port = os.getenv("POSTGRES_PORT") or "5432"
    auth = f"{user}:{password}@" if user or password else ""
    return f"postgresql://{auth}{host}:{port}/{quote_plus(dbname)}"


def resolve_db_url(cli_url: str | None, env_file: str | None = None) -> str | None:
    if cli_url:
        return cli_url

    if env_file:
        load_dotenv(env_file)
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            return db_url

    env_path = find_dotenv(usecwd=True)
    if env_path:
        load_dotenv(env_path)
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            return db_url

    return _build_libpq_url() or _build_postgres_env_url()


def parse_checks(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if name not in ALL_CHECKS:
            valid = ", ".join(ALL_CHECKS)
            raise ValueError(f"Unknown check '{name}'. Valid checks: {valid}")
        if name not in names:
            names.append(name)
    return names or None


def normalize_schemas(raw: list[str] | None) -> list[str] | None:
    if not raw:
        return None
    result: list[str] = []
    for item in raw:
        for part in item.split(","):
            part = part.strip()
            if part and part not in result:
                result.append(part)
    return result or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sql-audit",
        description="Centralized PostgreSQL Index and Storage Auditor",
    )
    parser.add_argument(
        "--url",
        help="Postgres connection string. If omitted, reads config from --env-file, "
        ".env, or PG* environment variables",
    )
    parser.add_argument(
        "--env-file",
        help="Path to a .env file containing DATABASE_URL",
    )
    parser.add_argument(
        "--checks",
        help="Comma-separated subset of checks to run: redundant, hot, low-usage "
        "(default: all)",
    )
    parser.add_argument(
        "--schema",
        action="append",
        help="Schema (namespace) to filter checks by. Repeatable or comma-separated",
    )
    parser.add_argument(
        "--min-size",
        type=int,
        default=0,
        help="Ignore indexes below this size in bytes (redundant and low-usage checks, "
        "default: 0)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON report to stdout",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the summary counts (text mode)",
    )
    parser.add_argument(
        "--canonical-json",
        action="store_true",
        help="Emit canonical domain AuditReport JSON with stable finding and evidence IDs",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_CONNECT_TIMEOUT,
        help=f"Connection timeout in seconds (default: {DEFAULT_CONNECT_TIMEOUT})",
    )
    parser.add_argument(
        "--min-hot-ratio",
        type=float,
        default=30.0,
        help="Minimum acceptable HOT ratio percentage (default: 30.0)",
    )
    parser.add_argument(
        "--min-updates",
        type=int,
        default=50,
        help="Minimum table updates to consider for HOT analysis (default: 50)",
    )
    parser.add_argument(
        "--max-rw-ratio",
        type=float,
        default=0.05,
        help="Maximum read/write ratio to flag an index as low-usage (default: 0.05)",
    )
    parser.add_argument(
        "--min-table-rows",
        type=int,
        default=10000,
        help="Only report HOT/low-usage issues for tables with at least N live rows "
        "(default: 10000); 0 disables the threshold",
    )
    return parser


def main(
    argv: list[str] | None = None,
    auditor_factory: Callable[..., Any] = PostgresHealthAuditor,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.min_table_rows < 0:
        print("Error: --min-table-rows must be >= 0", file=sys.stderr)
        return 1

    try:
        checks = parse_checks(args.checks)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    schemas = normalize_schemas(args.schema)

    db_url = resolve_db_url(args.url, args.env_file)
    if not db_url:
        print(
            "Error: no database configuration found. Provide --url, --env-file, "
            "a .env file, or standard PG* environment variables.",
            file=sys.stderr,
        )
        return 1

    selected_checks = checks if checks is not None else list(ALL_CHECKS)
    auditor = auditor_factory(db_url=db_url, connect_timeout=args.timeout)
    try:
        report = auditor.run_audit(
            min_hot_ratio_pct=args.min_hot_ratio,
            min_updates_threshold=args.min_updates,
            max_rw_ratio=args.max_rw_ratio,
            checks=selected_checks,
            schemas=schemas,
            min_size_bytes=args.min_size,
            min_table_rows=args.min_table_rows,
        )
    except DatabaseConnectionError as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to audit database: {exc}", file=sys.stderr)
        return 1

    if args.canonical_json:
        audit_report = report.to_audit_report(
            database=redact_db_url(db_url),
            checks=selected_checks,
            schemas=schemas,
            min_size_bytes=args.min_size,
            min_table_rows=args.min_table_rows,
        )
        print(audit_report.model_dump_json(indent=2))
    elif args.json:
        print(
            json.dumps(
                render_json(report, database=redact_db_url(db_url), checks=selected_checks)
            )
        )
    elif args.quiet:
        print(render_quiet(report))
    else:
        print_report(report)

    if report.has_critical_issues:
        return 3
    return 2 if has_issues(report) else 0


if __name__ == "__main__":
    sys.exit(main())
