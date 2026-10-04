# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "asyncpg>=0.29.0",
#     "mcp>=1.0.0,<2",
#     "pydantic>=2.0.0",
#     "python-dotenv>=1.0.0",
# ]
# ///
"""Asynchronous PostgreSQL health auditor MCP server and runner.

Exposes deterministic database health checks, lock contention graphs,
physical bloat estimations, and execution plan simulation over FastMCP.
"""

import os
import sys
import time

import asyncpg
from dotenv import find_dotenv, load_dotenv
from mcp.server.fastmcp import FastMCP

from sql_audit.application import (
    render_bloat_report_text,
    render_lock_report_text,
    render_plan_report_text,
)
from sql_audit.domain import (
    DEFAULT_MIN_SIZE_BYTES,
    DEFAULT_MIN_TABLE_ROWS,
    BloatReport,
    CheckName,
    DeadTuplesIssue,
    HotUpdateIssue,
    InvalidIndexIssue,
    LockContentionReport,
    LowUsageIndexIssue,
    PlanAnalysisReport,
    PostgresHealthReport,
    RedundantIndexIssue,
    UnindexedFKIssue,
)
from sql_audit.infrastructure.async_auditor import AsyncPostgresHealthAuditor

# Load environment variables from .env if present
load_dotenv(find_dotenv(usecwd=True))

__all__ = [
    "AsyncPostgresHealthAuditor",
    "CheckName",
    "DeadTuplesIssue",
    "HotUpdateIssue",
    "InvalidIndexIssue",
    "LowUsageIndexIssue",
    "PostgresHealthReport",
    "RedundantIndexIssue",
    "UnindexedFKIssue",
    "asyncpg",
    "mcp",
    "pg_bloat",
    "pg_explain",
    "pg_health_audit",
    "pg_locks",
    "resolve_dsn",
    "run_mcp_cli",
]

# --- FastMCP Server Definition ---

mcp = FastMCP("PostgreSQL Health Auditor")


def resolve_dsn(db_alias: str) -> str:
    """Resolves DSN from environment without exposing credentials to MCP protocol."""
    env_var = f"DB_{db_alias.upper()}_URL" if db_alias.lower() != "default" else "DATABASE_URL"
    dsn = os.getenv(env_var)
    if not dsn:
        raise ValueError(
            f"Database connection string not found for alias '{db_alias}'. "
            f"Please set environment variable '{env_var}'."
        )
    return dsn


@mcp.tool(
    name="pg_health_audit",
    description=(
        "Performs a deterministic audit of up to 6 PostgreSQL performance anti-patterns: "
        "invalid indexes, unindexed foreign keys, autovacuum/dead tuple lag, "
        "broken HOT updates, redundant indexes, and low-usage/unprofitable indexes. "
        "Defaults to auditing the 'public' schema when no schemas are specified."
    ),
)
async def pg_health_audit(
    schemas: list[str] | None = None,
    enabled_checks: list[CheckName] | None = None,
    min_table_rows: int = DEFAULT_MIN_TABLE_ROWS,
    min_size_bytes: int = DEFAULT_MIN_SIZE_BYTES,
    db_alias: str = "default",
    connect_timeout: int | None = None,
) -> PostgresHealthReport:
    """Deterministic MCP tool with flat parameters and environment-backed DSN resolution."""
    start_time = time.perf_counter()
    dsn = resolve_dsn(db_alias)
    effective_schemas = schemas if schemas is not None else ["public"]

    checks_to_run = (
        [c.value for c in enabled_checks]
        if enabled_checks is not None
        else [c.value for c in CheckName]
    )

    auditor = AsyncPostgresHealthAuditor(
        dsn=dsn,
        connect_timeout=connect_timeout if connect_timeout is not None else 10,
    )

    report = await auditor.run_full_audit(
        schemas=effective_schemas,
        checks=checks_to_run,
        min_table_rows=min_table_rows,
        min_size_bytes=min_size_bytes,
        db_alias=db_alias,
    )
    report.execution_time_ms = round((time.perf_counter() - start_time) * 1000, 2)
    return report


@mcp.tool(
    name="pg_locks",
    description=(
        "Analyzes active lock contention and reconstructs blocking transaction trees in PostgreSQL."
    ),
)
async def pg_locks(
    db_alias: str = "default",
    connect_timeout: int | None = None,
) -> LockContentionReport:
    """Detects blocked queries and active lock dependency trees."""
    dsn = resolve_dsn(db_alias)
    auditor = AsyncPostgresHealthAuditor(
        dsn=dsn,
        connect_timeout=connect_timeout if connect_timeout is not None else 10,
    )
    return await auditor.audit_locks()


@mcp.tool(
    name="pg_bloat",
    description=(
        "Estimates physical dead-space bloat in PostgreSQL tables and B-tree indexes from "
        "catalog statistics. Defaults to auditing the 'public' schema when no schemas "
        "are specified."
    ),
)
async def pg_bloat(
    schemas: list[str] | None = None,
    min_bloat_bytes: int = 10_000_000,
    min_bloat_ratio_pct: float = 20.0,
    db_alias: str = "default",
    connect_timeout: int | None = None,
) -> BloatReport:
    """Estimates wasted physical disk pages across tables and indexes."""
    dsn = resolve_dsn(db_alias)
    effective_schemas = schemas if schemas is not None else ["public"]
    auditor = AsyncPostgresHealthAuditor(
        dsn=dsn,
        connect_timeout=connect_timeout if connect_timeout is not None else 10,
    )
    return await auditor.audit_bloat(
        schemas=effective_schemas,
        min_bloat_bytes=min_bloat_bytes,
        min_bloat_ratio_pct=min_bloat_ratio_pct,
        db_alias=db_alias,
    )


@mcp.tool(
    name="pg_explain",
    description=(
        "Simulates and audits a PostgreSQL execution plan using static EXPLAIN "
        "(COSTS, VERBOSE, FORMAT JSON). Never runs EXPLAIN ANALYZE or executes user queries."
    ),
)
async def pg_explain(
    query: str,
    db_alias: str = "default",
    connect_timeout: int | None = None,
) -> PlanAnalysisReport:
    """Simulates and audits a PostgreSQL execution plan."""
    dsn = resolve_dsn(db_alias)
    auditor = AsyncPostgresHealthAuditor(
        dsn=dsn,
        connect_timeout=connect_timeout if connect_timeout is not None else 10,
    )
    return await auditor.explain_query(query=query, db_alias=db_alias)


async def _run_cli_main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Deterministic PostgreSQL Health Auditor (CLI mode)"
    )
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN (or via DATABASE_URL)")
    parser.add_argument("--alias", default="default", help="Database alias for DB_{ALIAS}_URL")
    parser.add_argument(
        "--schemas",
        default="public",
        help="Comma-separated list of schemas (default: public)",
    )
    parser.add_argument(
        "--min-table-rows",
        type=int,
        default=DEFAULT_MIN_TABLE_ROWS,
        help=f"Min live rows in table to evaluate (default: {DEFAULT_MIN_TABLE_ROWS})",
    )
    parser.add_argument(
        "--min-size-bytes",
        type=int,
        default=DEFAULT_MIN_SIZE_BYTES,
        help=f"Min index size in bytes (default: {DEFAULT_MIN_SIZE_BYTES})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Connection timeout in seconds (default: 10)",
    )
    parser.add_argument("--json", action="store_true", help="Output full report as formatted JSON")
    parser.add_argument(
        "--locks", action="store_true", help="Inspect lock contention and blocking trees"
    )
    parser.add_argument(
        "--bloat", action="store_true", help="Estimate physical bloat in tables and indexes"
    )
    parser.add_argument("--explain", default=None, help="Query string to explain and audit")

    raw_args = [a for a in sys.argv[1:] if a not in ("--cli", "-c")]
    args = parser.parse_args(raw_args)

    from audit_pg import resolve_db_url  # local import avoids circular import at module load

    dsn = resolve_db_url(cli_url=args.dsn)
    if not dsn:
        dsn = resolve_dsn(args.alias)

    auditor = AsyncPostgresHealthAuditor(dsn=dsn, connect_timeout=args.timeout)

    if args.locks:
        lock_report = await auditor.audit_locks()
        if args.json:
            print(lock_report.model_dump_json(indent=2))
        else:
            print(render_lock_report_text(lock_report))
        return

    if args.bloat:
        schemas = [s.strip() for s in args.schemas.split(",") if s.strip()]
        bloat_report = await auditor.audit_bloat(
            schemas=schemas,
            min_bloat_bytes=args.min_size_bytes,
            db_alias=args.alias,
        )
        if args.json:
            print(bloat_report.model_dump_json(indent=2))
        else:
            print(render_bloat_report_text(bloat_report))
        return

    if args.explain:
        plan_report = await auditor.explain_query(
            query=args.explain,
            db_alias=args.alias,
        )
        if args.json:
            print(plan_report.model_dump_json(indent=2))
        else:
            print(render_plan_report_text(plan_report))
        return

    schemas = [s.strip() for s in args.schemas.split(",") if s.strip()]

    start_time = time.perf_counter()
    report = await auditor.run_full_audit(
        schemas=schemas,
        checks=[c.value for c in CheckName],
        min_table_rows=args.min_table_rows,
        min_size_bytes=args.min_size_bytes,
        db_alias=args.alias,
    )
    report.execution_time_ms = round((time.perf_counter() - start_time) * 1000, 2)

    if args.json:
        print(report.model_dump_json(indent=2))
        return

    _print_cli_summary(report)


def _print_cli_summary(report: PostgresHealthReport) -> None:
    print("\n=======================================================")
    print(f" PostgreSQL Health Audit Report ({report.database_alias})")
    print(
        f" Execution time: {report.execution_time_ms} ms | "
        f"Schemas: {', '.join(report.schemas_audited)}"
    )
    print(f" Critical issues detected: {'YES' if report.has_critical_issues else 'NO'}")
    print("=======================================================\n")

    print(f"[1] Invalid Indexes (indisvalid = false): {len(report.invalid_indexes)}")
    for inv in report.invalid_indexes:
        print(
            f"    - Table: {inv.child_table} | Index: {inv.invalid_index} (Size: {inv.index_size})"
        )

    print(f"[2] Unindexed Foreign Keys: {len(report.unindexed_fks)}")
    for fk in report.unindexed_fks:
        print(f"    - FK: {fk.fk_name} on {fk.child_table} -> {fk.parent_table}")
        print(f"      Def: {fk.fk_definition}")

    print(f"[3] Autovacuum & Dead Tuples Lag: {len(report.autovacuum_dead_tuples)}")
    for dt in report.autovacuum_dead_tuples:
        print(
            f"    - Table: {dt.table_name} | "
            f"Dead tuples: {dt.dead_tuples:,} ({dt.dead_tuple_pct}%) | Live: {dt.live_tuples:,}"
        )

    print(f"[4] Broken HOT Updates / Fillfactor Issues: {len(report.hot_fillfactor_issues)}")
    for hot in report.hot_fillfactor_issues:
        print(
            f"    - Table: {hot.table_name} | "
            f"HOT ratio: {hot.hot_ratio_pct}% ({hot.hot_updates}/{hot.total_updates}) | "
            f"Fillfactor: {hot.fillfactor}"
        )

    print(f"[5] Redundant / Duplicate Indexes: {len(report.redundant_indexes)}")
    for red in report.redundant_indexes:
        print(
            f"    - Table: {red.table_name} | "
            f"Redundant: {red.redundant_index} ({red.redundant_size}) | "
            f"Covered by: {red.covering_index}"
        )

    print(f"[6] Low Usage / Unprofitable Indexes: {len(report.low_usage_indexes)}")
    for low in report.low_usage_indexes:
        print(
            f"    - Table: {low.table_name} | Index: {low.index_name} ({low.size}) | "
            f"Scans: {low.index_scans} vs Writes: {low.table_writes} | "
            f"Ratio: {low.read_write_ratio}"
        )

    print("\nAudit completed.\n")


def run_mcp_cli() -> None:
    """Sync entry point for [project.scripts] — asyncio wrapper for _run_cli_main."""
    import asyncio

    asyncio.run(_run_cli_main())


if __name__ == "__main__":
    if "--cli" in sys.argv or "-c" in sys.argv:
        import asyncio

        asyncio.run(_run_cli_main())
    else:
        mcp.run(transport="stdio")
