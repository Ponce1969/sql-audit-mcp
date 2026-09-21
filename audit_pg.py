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
from decimal import Decimal
from typing import Any
from urllib.parse import quote_plus, urlsplit

from dotenv import find_dotenv, load_dotenv

try:
    import psycopg2
    import psycopg2.extras

    HAS_PSYCOPG2 = True
except ImportError:  # pragma: no cover
    HAS_PSYCOPG2 = False


ALL_CHECKS = ["redundant", "hot", "low-usage"]
CHECK_FIELDS: dict[str, str] = {
    "redundant": "redundant_indexes",
    "hot": "hot_issues",
    "low-usage": "low_usage_indexes",
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
class DatabaseHealthReport:
    hot_issues: list[HotUpdateIssue] = field(default_factory=list)
    redundant_indexes: list[RedundantIndexIssue] = field(default_factory=list)
    low_usage_indexes: list[LowUsageIndexIssue] = field(default_factory=list)


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

                return DatabaseHealthReport(
                    hot_issues=hot_issues,
                    redundant_indexes=redundant_indexes,
                    low_usage_indexes=low_usage,
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
        query = """
        SELECT
            t.relname AS table_name,
            t.n_tup_upd AS total_updates,
            t.n_tup_hot_upd AS hot_updates,
            ROUND((t.n_tup_hot_upd::numeric / NULLIF(t.n_tup_upd, 0)) * 100, 2) AS hot_ratio_pct,
            COALESCE(
                (
                    SELECT split_part(opt, '=', 2)::int
                    FROM unnest(c.reloptions) AS opt
                    WHERE substr(opt, 1, 11) = 'fillfactor='
                    LIMIT 1
                ), 100
            ) AS fillfactor,
            t.n_live_tup AS table_rows,
            pg_size_pretty(pg_total_relation_size(t.relid)) AS table_size
        FROM pg_stat_user_tables t
        JOIN pg_class c ON c.oid = t.relid
        JOIN pg_namespace ns ON ns.oid = c.relnamespace
        WHERE t.n_tup_upd >= %s
          AND ROUND((t.n_tup_hot_upd::numeric / NULLIF(t.n_tup_upd, 0)) * 100, 2) < %s
          AND t.n_live_tup >= %s
        """
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
        # Detects exact duplicate indexes and left-prefix redundant indexes using CTE
        query = """
        WITH parsed_indexes AS (
            SELECT
                i.indexrelid,
                i.indrelid,
                i.indisunique,
                i.indisprimary,
                i.indpred,
                string_to_array(i.indkey::text, ' ') AS keys,
                pg_relation_size(i.indexrelid) AS size_bytes
            FROM pg_index i
        )
        SELECT
            c.relname AS table_name,
            idx.relname AS redundant_index,
            pg_size_pretty(p1.size_bytes) AS redundant_size,
            lead_idx.relname AS covering_index,
            pg_get_indexdef(p1.indexrelid) AS redundant_def,
            pg_get_indexdef(p2.indexrelid) AS covering_def
        FROM parsed_indexes p1
        JOIN parsed_indexes p2 ON p1.indrelid = p2.indrelid AND p1.indexrelid != p2.indexrelid
        JOIN pg_class idx ON idx.oid = p1.indexrelid
        JOIN pg_class c ON c.oid = p1.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_class lead_idx ON lead_idx.oid = p2.indexrelid
        WHERE n.nspname NOT IN ('pg_catalog', 'pg_toast')
          AND NOT p1.indisunique
          AND NOT p1.indisprimary
          AND p1.indpred IS NULL
          AND '0' != ALL(p1.keys)
          AND '0' != ALL(p2.keys)
          AND p2.keys[1:cardinality(p1.keys)] = p1.keys
          AND p1.size_bytes >= %s
        """
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
        query = """
        SELECT
            i.relname AS table_name,
            i.indexrelname AS index_name,
            pg_size_pretty(pg_relation_size(i.indexrelid)) AS size,
            i.idx_scan AS index_scans,
            (t.n_tup_ins + t.n_tup_upd + t.n_tup_del) AS table_writes,
            ROUND(
                i.idx_scan::numeric / NULLIF(t.n_tup_ins + t.n_tup_upd + t.n_tup_del, 0), 4
            ) AS read_write_ratio,
            t.n_live_tup AS table_rows,
            pg_size_pretty(pg_total_relation_size(t.relid)) AS table_size
        FROM pg_stat_user_indexes i
        JOIN pg_stat_user_tables t ON i.relid = t.relid
        JOIN pg_index idx ON idx.indexrelid = i.indexrelid
        JOIN pg_class c ON c.oid = i.relid
        JOIN pg_namespace ns ON ns.oid = c.relnamespace
        WHERE (t.n_tup_ins + t.n_tup_upd + t.n_tup_del) > 100
          AND NOT idx.indisprimary
          AND NOT idx.indisunique
          AND (i.idx_scan::numeric / NULLIF(t.n_tup_ins + t.n_tup_upd + t.n_tup_del, 0)) < %s
          AND pg_relation_size(i.indexrelid) >= %s
          AND t.n_live_tup >= %s
        """
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


def render_text(report: DatabaseHealthReport) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 65)
    lines.append("        POSTGRESQL STORAGE & INDEX HEALTH REPORT")
    lines.append("=" * 65)

    # 1. Redundant Indexes
    lines.append("")
    lines.append(f"[1] REDUNDANT / PREFIX INDEXES FOUND: {len(report.redundant_indexes)}")
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

    # 2. HOT Updates & Fillfactor
    lines.append(f"[2] LOW HOT UPDATE EFFICIENCY: {len(report.hot_issues)}")
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

    # 3. Low Usage Indexes
    lines.append(f"[3] UNPROFITABLE / HIGH-WRITE LOW-READ INDEXES: {len(report.low_usage_indexes)}")
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
        f"redundant_indexes: {len(report.redundant_indexes)}",
        f"hot_issues: {len(report.hot_issues)}",
        f"low_usage_indexes: {len(report.low_usage_indexes)}",
    ]
    return "\n".join(lines)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
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
        "summary": {
            "redundant_indexes": len(report.redundant_indexes),
            "hot_issues": len(report.hot_issues),
            "low_usage_indexes": len(report.low_usage_indexes),
        },
        "issues": issues,
    }


def has_issues(report: DatabaseHealthReport) -> bool:
    return bool(report.hot_issues or report.redundant_indexes or report.low_usage_indexes)


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

    if args.json:
        print(
            json.dumps(
                render_json(report, database=redact_db_url(db_url), checks=selected_checks)
            )
        )
    elif args.quiet:
        print(render_quiet(report))
    else:
        print_report(report)

    return 2 if has_issues(report) else 0


if __name__ == "__main__":
    sys.exit(main())
