# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "psycopg2-binary>=2.9.0",
#     "pydantic>=2.0.0",
#     "python-dotenv>=1.0.0",
# ]
# ///
"""Synchronous PostgreSQL health auditor CLI and presentation adapter.

Entry point: sql-audit (via pyproject.toml [project.scripts]).
See README.md for full usage guide.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote_plus

from dotenv import find_dotenv, load_dotenv

from sql_audit.application import (
    AuditReport,
    compare_audit_reports,
    render_bloat_report_text,
    render_diff_text,
    render_lock_report_text,
    render_plan_report_text,
)
from sql_audit.domain import (
    ALL_CHECKS,
    CHECK_FIELDS,
    DEFAULT_MIN_SIZE_BYTES,
    DEFAULT_MIN_TABLE_ROWS,
    DatabaseHealthReport,
    DeadTuplesIssue,
    HotUpdateIssue,
    InvalidIndexIssue,
    LowUsageIndexIssue,
    PlanRiskLevel,
    RedundantIndexIssue,
    Severity,
    UnindexedFKIssue,
)
from sql_audit.infrastructure.sync_auditor import (
    DEFAULT_CONNECT_TIMEOUT,
    HAS_PSYCOPG2,
    DatabaseConnectionError,
    PostgresHealthAuditor,
    redact_db_url,
)

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]

__all__ = [
    "ALL_CHECKS",
    "CHECK_FIELDS",
    "DEFAULT_CONNECT_TIMEOUT",
    "DatabaseConnectionError",
    "DatabaseHealthReport",
    "DeadTuplesIssue",
    "HAS_PSYCOPG2",
    "HotUpdateIssue",
    "InvalidIndexIssue",
    "LowUsageIndexIssue",
    "PostgresHealthAuditor",
    "RedundantIndexIssue",
    "UnindexedFKIssue",
    "build_parser",
    "has_issues",
    "main",
    "normalize_schemas",
    "parse_checks",
    "print_report",
    "redact_db_url",
    "render_json",
    "render_quiet",
    "render_text",
    "resolve_db_url",
]


def render_text(report: DatabaseHealthReport) -> str:
    """Renders human-readable plain text report of all 6 health checks."""
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 65)
    lines.append("        POSTGRESQL STORAGE & INDEX HEALTH REPORT")
    lines.append("=" * 65)

    if report.is_partial or report.errors:
        lines.append("")
        lines.append("  [!] WARNING: PARTIAL AUDIT (DEGRADED EXECUTION)")
        for err in report.errors:
            lines.append(f"      * Check failure: {err}")
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
                "    - Diagnostic: Index is marked invalid (indisvalid = false) "
                "from a failed build."
            )
            lines.append(
                "    - Considerations: Consumes storage and write overhead "
                "while ignored by the planner; "
            )
            lines.append(
                "      evaluate cleanup and rebuild requirements during maintenance windows."
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
                "    - Diagnostic: Foreign key constraint lacks a supporting "
                "index on referencing columns."
            )
            lines.append(
                "    - Considerations: Parent table updates/deletes may acquire "
                "share locks on child table; "
            )
            lines.append(
                "      evaluate indexing referencing columns to prevent lock contention."
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
                "    - Diagnostic: Excessive dead tuple accumulation indicates "
                "autovacuum lag or starvation."
            )
            lines.append(
                "    - Considerations: Investigate uncommitted long-running transactions or "
                "autovacuum settings preventing dead tuple reclamation."
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
            lines.append(
                f"    - Diagnostic: Index is a left-prefix duplicate covered by "
                f"'{idx.covering_index}'."
            )
            lines.append(
                "    - Considerations: Consumes cache and write overhead redundantly; "
                "verify query usage "
            )
            lines.append(
                "      and constraint requirements before planning index retirement."
            )
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
            lines.append(f"    - Rows: {issue.table_rows} | Table size: {issue.table_size}")
            lines.append(f"    - Fillfactor: {issue.fillfactor}%{warning}")
            if issue.fillfactor_warning:
                lines.append(
                    "    - Diagnostic: Default 100% fillfactor leaves no page headroom "
                    "for HOT updates."
                )
                lines.append(
                    "    - Considerations: Frequent non-indexed updates trigger heap bloat and "
                    "WAL amplification; evaluate lowering fillfactor and scheduling compaction "
                    "during maintenance windows."
                )
                lines.append("")

    # 6. Low Usage Indexes
    lines.append(f"[6] UNPROFITABLE / HIGH-WRITE LOW-READ INDEXES: {len(report.low_usage_indexes)}")
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
            lines.append(
                "    - Diagnostic: High write maintenance overhead with "
                "negligible scan utilization."
            )
            lines.append(
                "    - Considerations: Verify query patterns, unique constraints, and operational "
                "requirements before considering index retirement."
            )
            lines.append("")

    lines.append("=" * 65)
    lines.append("")
    return "\n".join(lines)


def print_report(report: DatabaseHealthReport) -> None:
    print(render_text(report))


def render_quiet(report: DatabaseHealthReport) -> str:
    """Renders concise line-based issue counts for quiet CLI mode."""
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
    """Coerces values like Decimal or datetime into standard JSON types."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def render_json(report: DatabaseHealthReport, database: str, checks: list[str]) -> dict[str, Any]:
    """Renders structured JSON dictionary matching the legacy CLI contract."""
    issues: dict[str, list[dict[str, Any]]] = {}
    for check in checks:
        field_name = CHECK_FIELDS[check]
        issues[check] = [
            {key: _jsonable(value) for key, value in asdict(issue).items()}
            for issue in getattr(report, field_name)
        ]
    data: dict[str, Any] = {
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
    if report.is_partial or report.errors:
        data["is_partial"] = report.is_partial
        data["checks_executed"] = report.checks_executed
        data["checks_failed"] = report.checks_failed
        data["errors"] = report.errors
    return data


def has_issues(report: DatabaseHealthReport) -> bool:
    """Returns True if the health report contains any findings, errors, or degraded checks."""
    return bool(
        report.is_partial
        or report.errors
        or report.hot_issues
        or report.redundant_indexes
        or report.low_usage_indexes
        or report.invalid_indexes
        or report.unindexed_fks
        or report.autovacuum_dead_tuples
    )


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
    """Resolves database connection URL from CLI arguments, .env file, or environment."""
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
        help="Comma-separated subset of checks to run: redundant, hot, low-usage (default: all)",
    )
    parser.add_argument(
        "--schema",
        action="append",
        help="Schema (namespace) to filter checks by. Repeatable or comma-separated",
    )
    parser.add_argument(
        "--min-size",
        type=int,
        default=DEFAULT_MIN_SIZE_BYTES,
        help=(
            "Ignore indexes below this size in bytes "
            f"(redundant and low-usage checks, default: {DEFAULT_MIN_SIZE_BYTES})"
        ),
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
        "--diff",
        help="Path to a previous AuditReport JSON file to compare against (regression detection)",
    )
    parser.add_argument(
        "--locks",
        action="store_true",
        help="Inspect active lock contention and reconstruct blocking transaction trees",
    )
    parser.add_argument(
        "--bloat",
        action="store_true",
        help="Estimate physical bloat for tables and B-tree indexes from catalog statistics",
    )
    parser.add_argument(
        "--min-bloat-bytes",
        type=int,
        default=10_000_000,
        help="Minimum estimated bloat size in bytes to report (default: 10 MB)",
    )
    parser.add_argument(
        "--min-bloat-ratio",
        type=float,
        default=20.0,
        help="Minimum estimated bloat percentage to report (default: 20.0)",
    )
    parser.add_argument(
        "--explain",
        help="SQL query string to run EXPLAIN (COSTS, VERBOSE, FORMAT JSON) against",
    )
    parser.add_argument(
        "--explain-file",
        help="Path to a SQL file containing query to explain",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Connection timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--min-hot-ratio",
        type=float,
        default=30.0,
        help="Minimum HOT update ratio threshold percentage (default: 30.0)",
    )
    parser.add_argument(
        "--min-updates",
        type=int,
        default=50,
        help="Minimum update count threshold to audit HOT ratio (default: 50)",
    )
    parser.add_argument(
        "--max-rw-ratio",
        type=float,
        default=0.05,
        help="Maximum read/write ratio threshold for low-usage indexes (default: 0.05)",
    )
    parser.add_argument(
        "--min-table-rows",
        type=int,
        default=DEFAULT_MIN_TABLE_ROWS,
        help=(
            "Minimum rows in table to report HOT or low-usage issues "
            f"(default: {DEFAULT_MIN_TABLE_ROWS})"
        ),
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

    auditor = auditor_factory(db_url=db_url, connect_timeout=args.timeout)

    if args.explain or args.explain_file:
        query_text = args.explain
        if args.explain_file:
            try:
                with open(args.explain_file, encoding="utf-8") as f:
                    query_text = f.read()
            except Exception as exc:  # noqa: BLE001
                print(f"Error reading SQL file: {exc}", file=sys.stderr)
                return 1

        if not query_text or not query_text.strip():
            print("Error: query text is empty", file=sys.stderr)
            return 1

        try:
            plan_report = auditor.explain_query(query=query_text)
        except ValueError as exc:
            print(f"Invalid query: {exc}", file=sys.stderr)
            return 1
        except DatabaseConnectionError as exc:
            print(f"Connection failed: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to explain query: {exc}", file=sys.stderr)
            return 1

        if args.canonical_json or args.json:
            print(plan_report.model_dump_json(indent=2))
        else:
            print(render_plan_report_text(plan_report))

        has_high_risk = any(
            w.risk_level in (PlanRiskLevel.HIGH, PlanRiskLevel.CRITICAL)
            for w in plan_report.summary.warnings
        )
        return 3 if has_high_risk else (2 if plan_report.summary.warnings else 0)

    if args.locks:
        try:
            lock_report = auditor.audit_locks()
        except DatabaseConnectionError as exc:
            print(f"Connection failed: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to inspect locks: {exc}", file=sys.stderr)
            return 1

        if args.canonical_json or args.json:
            print(lock_report.model_dump_json(indent=2))
        else:
            print(render_lock_report_text(lock_report))

        if lock_report.total_blocked_processes > 0:
            return 3
        return 0

    if args.bloat:
        try:
            bloat_report = auditor.audit_bloat(
                schemas=schemas,
                min_bloat_bytes=args.min_bloat_bytes,
                min_bloat_ratio_pct=args.min_bloat_ratio,
            )
        except DatabaseConnectionError as exc:
            print(f"Connection failed: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to estimate bloat: {exc}", file=sys.stderr)
            return 1

        if args.canonical_json or args.json:
            print(bloat_report.model_dump_json(indent=2))
        else:
            print(render_bloat_report_text(bloat_report))

        has_bloat = any(t.is_bloated for t in bloat_report.tables) or any(
            i.is_bloated for i in bloat_report.indexes
        )
        return 2 if has_bloat else 0

    selected_checks = checks if checks is not None else list(ALL_CHECKS)
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

    if args.diff:
        try:
            with open(args.diff, encoding="utf-8") as f:
                prev_data = json.load(f)
            prev_report = AuditReport.model_validate(prev_data)
        except Exception as exc:  # noqa: BLE001
            print(f"Error loading previous audit report: {exc}", file=sys.stderr)
            return 1

        curr_report = report.to_audit_report(
            database=redact_db_url(db_url),
            checks=selected_checks,
            schemas=schemas,
            min_size_bytes=args.min_size,
            min_table_rows=args.min_table_rows,
        )
        diff = compare_audit_reports(prev_report, curr_report)

        if args.canonical_json or args.json:
            print(diff.model_dump_json(indent=2))
        else:
            print(render_diff_text(diff))

        has_new_critical = any(f.severity == Severity.CRITICAL for f in diff.new_findings)
        if has_new_critical:
            return 3
        return 2 if (diff.new_findings or diff.changed_findings) else 0

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
            json.dumps(render_json(report, database=redact_db_url(db_url), checks=selected_checks))
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
