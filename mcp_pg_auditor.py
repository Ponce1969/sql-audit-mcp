# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "asyncpg>=0.29.0",
#     "mcp>=1.0.0,<2",
#     "pydantic>=2.0.0",
#     "python-dotenv>=1.0.0",
# ]
# ///

import os
import sys
import time
from datetime import datetime
from enum import Enum

import asyncpg
from dotenv import find_dotenv, load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from sql_audit.application import (
    AuditReport,
    analyze_execution_plan,
    build_bloat_report,
    build_lock_contention_report,
    convert_legacy_report_to_audit_report,
    render_bloat_report_text,
    render_lock_report_text,
    render_plan_report_text,
)
from sql_audit.domain import BloatReport, LockContentionReport, PlanAnalysisReport
from sql_audit.infrastructure.queries import (
    SQL_DEAD_TUPLES_ASYNCPG,
    SQL_HOT_ASYNCPG,
    SQL_INDEX_BLOAT_ASYNCPG,
    SQL_INVALID_INDEXES_ASYNCPG,
    SQL_LOCK_CONTENTION,
    SQL_LOW_USAGE_ASYNCPG,
    SQL_REDUNDANT_ASYNCPG,
    SQL_TABLE_BLOAT_ASYNCPG,
    SQL_UNINDEXED_FKS_ASYNCPG,
)

# Load environment variables from .env if present
load_dotenv(find_dotenv(usecwd=True))


class CheckName(str, Enum):
    INVALID_INDEXES = "invalid_indexes"
    UNINDEXED_FKS = "unindexed_fks"
    AUTOVACUUM_DEAD_TUPLES = "autovacuum_dead_tuples"
    HOT_FILLFACTOR = "hot_fillfactor"
    REDUNDANT_INDEXES = "redundant_indexes"
    LOW_USAGE_INDEXES = "low_usage_indexes"


# --- Issue DTOs (Pydantic v2) ---


class InvalidIndexIssue(BaseModel):
    child_table: str
    invalid_index: str
    index_size: str


class UnindexedFKIssue(BaseModel):
    child_table: str
    fk_name: str
    parent_table: str
    fk_definition: str


class DeadTuplesIssue(BaseModel):
    table_name: str
    dead_tuples: int
    live_tuples: int
    dead_tuple_pct: float
    last_autovacuum: datetime | None = None
    last_vacuum: datetime | None = None


class HotUpdateIssue(BaseModel):
    table_name: str
    total_updates: int
    hot_updates: int
    hot_ratio_pct: float
    fillfactor: int
    fillfactor_warning: bool
    table_rows: int
    table_size: str


class RedundantIndexIssue(BaseModel):
    table_name: str
    redundant_index: str
    redundant_size: str
    covering_index: str
    redundant_def: str
    covering_def: str


class LowUsageIndexIssue(BaseModel):
    table_name: str
    index_name: str
    size: str
    index_scans: int
    table_writes: int
    read_write_ratio: float
    table_rows: int
    table_size: str


# --- Consolidated Health Report ---


class PostgresHealthReport(BaseModel):
    database_alias: str
    schemas_audited: list[str]
    execution_time_ms: float = 0.0
    has_critical_issues: bool = False
    invalid_indexes: list[InvalidIndexIssue] = Field(default_factory=list)
    unindexed_fks: list[UnindexedFKIssue] = Field(default_factory=list)
    autovacuum_dead_tuples: list[DeadTuplesIssue] = Field(default_factory=list)
    hot_fillfactor_issues: list[HotUpdateIssue] = Field(default_factory=list)
    redundant_indexes: list[RedundantIndexIssue] = Field(default_factory=list)
    low_usage_indexes: list[LowUsageIndexIssue] = Field(default_factory=list)

    def to_audit_report(
        self,
        checks: list[str] | None = None,
        observed_at: datetime | None = None,
        server_version: str = "unknown",
        min_size_bytes: int = 10_000_000,
        min_table_rows: int = 1000,
        audit_id: str | None = None,
    ) -> AuditReport:
        return convert_legacy_report_to_audit_report(
            report=self,
            database=self.database_alias,
            checks=checks or [c.value for c in CheckName],
            observed_at=observed_at,
            server_version=server_version,
            duration_ms=self.execution_time_ms,
            schemas=self.schemas_audited,
            min_size_bytes=min_size_bytes,
            min_table_rows=min_table_rows,
            audit_id=audit_id,
        )


# --- Asynchronous PostgreSQL Health Auditor ---


class AsyncPostgresHealthAuditor:
    """Performs deterministic catalog audits for PostgreSQL anti-patterns."""

    def __init__(self, dsn: str, connect_timeout: int = 10):
        self.dsn = dsn
        self.connect_timeout = connect_timeout

    async def run_full_audit(
        self,
        schemas: list[str],
        checks: list[str],
        min_table_rows: int = 1000,
        min_size_bytes: int = 10_000_000,
        db_alias: str = "default",
    ) -> PostgresHealthReport:
        conn = await asyncpg.connect(self.dsn, timeout=self.connect_timeout)
        try:
            invalid_indexes: list[InvalidIndexIssue] = []
            unindexed_fks: list[UnindexedFKIssue] = []
            dead_tuples: list[DeadTuplesIssue] = []
            hot_issues: list[HotUpdateIssue] = []
            redundant: list[RedundantIndexIssue] = []
            low_usage: list[LowUsageIndexIssue] = []

            if CheckName.INVALID_INDEXES.value in checks:
                invalid_indexes = await self._audit_invalid_indexes(conn, schemas)
            if CheckName.UNINDEXED_FKS.value in checks:
                unindexed_fks = await self._audit_unindexed_fks(conn, schemas)
            if CheckName.AUTOVACUUM_DEAD_TUPLES.value in checks:
                dead_tuples = await self._audit_autovacuum_dead_tuples(conn, schemas)
            if CheckName.HOT_FILLFACTOR.value in checks:
                hot_issues = await self._audit_hot_and_fillfactor(conn, schemas, min_table_rows)
            if CheckName.REDUNDANT_INDEXES.value in checks:
                redundant = await self._audit_redundant_indexes(conn, schemas, min_size_bytes)
            if CheckName.LOW_USAGE_INDEXES.value in checks:
                low_usage = await self._audit_low_usage_indexes(
                    conn, schemas, min_size_bytes, min_table_rows
                )

            has_critical = bool(invalid_indexes or unindexed_fks or dead_tuples)

            return PostgresHealthReport(
                database_alias=db_alias,
                schemas_audited=schemas,
                has_critical_issues=has_critical,
                invalid_indexes=invalid_indexes,
                unindexed_fks=unindexed_fks,
                autovacuum_dead_tuples=dead_tuples,
                hot_fillfactor_issues=hot_issues,
                redundant_indexes=redundant,
                low_usage_indexes=low_usage,
            )
        finally:
            await conn.close()

    async def audit_locks(self) -> LockContentionReport:
        """Inspects active lock contention and reconstructs blocking trees."""
        conn = await asyncpg.connect(self.dsn, timeout=self.connect_timeout)
        try:
            records = await conn.fetch(SQL_LOCK_CONTENTION)
            rows = [dict(r) for r in records]
            return build_lock_contention_report(rows)
        finally:
            await conn.close()

    async def audit_bloat(
        self,
        schemas: list[str] | None = None,
        min_bloat_bytes: int = 10_000_000,
        min_bloat_ratio_pct: float = 20.0,
        db_alias: str = "default",
    ) -> BloatReport:
        """Estimates physical bloat for tables and B-tree indexes from catalog statistics."""
        effective_schemas = schemas if schemas is not None else ["public"]
        conn = await asyncpg.connect(self.dsn, timeout=self.connect_timeout)
        try:
            tbl_records = await conn.fetch(SQL_TABLE_BLOAT_ASYNCPG, effective_schemas)
            idx_records = await conn.fetch(SQL_INDEX_BLOAT_ASYNCPG, effective_schemas)
            tbl_rows = [dict(r) for r in tbl_records]
            idx_rows = [dict(r) for r in idx_records]
            return build_bloat_report(
                table_rows=tbl_rows,
                index_rows=idx_rows,
                database=db_alias,
                min_bloat_bytes=min_bloat_bytes,
                min_bloat_ratio_pct=min_bloat_ratio_pct,
            )
        finally:
            await conn.close()

    async def explain_query(
        self,
        query: str,
        analyze: bool = False,
        db_alias: str = "default",
    ) -> PlanAnalysisReport:
        """Runs EXPLAIN (or safe transactional EXPLAIN ANALYZE) and audits the execution plan."""
        import json

        conn = await asyncpg.connect(self.dsn, timeout=self.connect_timeout)
        try:
            if analyze:
                # Transactional rollback ensures safety against mutating DML
                tx = conn.transaction()
                await tx.start()
                try:
                    explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, COSTS, VERBOSE, FORMAT JSON) {query}"
                    raw = await conn.fetchval(explain_sql)
                finally:
                    await tx.rollback()
            else:
                explain_sql = f"EXPLAIN (BUFFERS, COSTS, VERBOSE, FORMAT JSON) {query}"
                raw = await conn.fetchval(explain_sql)

            plan_data = json.loads(raw) if isinstance(raw, str) else raw
            return analyze_execution_plan(
                raw_plan_data=plan_data,
                query=query,
                database=db_alias,
            )
        finally:
            await conn.close()

    async def _audit_invalid_indexes(
        self, conn: asyncpg.Connection, schemas: list[str]
    ) -> list[InvalidIndexIssue]:
        rows = await conn.fetch(SQL_INVALID_INDEXES_ASYNCPG, schemas)
        return [InvalidIndexIssue(**dict(r)) for r in rows]

    async def _audit_unindexed_fks(
        self, conn: asyncpg.Connection, schemas: list[str]
    ) -> list[UnindexedFKIssue]:
        rows = await conn.fetch(SQL_UNINDEXED_FKS_ASYNCPG, schemas)
        return [UnindexedFKIssue(**dict(r)) for r in rows]

    async def _audit_autovacuum_dead_tuples(
        self, conn: asyncpg.Connection, schemas: list[str]
    ) -> list[DeadTuplesIssue]:
        rows = await conn.fetch(SQL_DEAD_TUPLES_ASYNCPG, schemas)
        return [DeadTuplesIssue(**dict(r)) for r in rows]

    async def _audit_hot_and_fillfactor(
        self,
        conn: asyncpg.Connection,
        schemas: list[str],
        min_table_rows: int,
        min_ratio: float = 30.0,
        min_updates: int = 50,
    ) -> list[HotUpdateIssue]:
        rows = await conn.fetch(SQL_HOT_ASYNCPG, min_updates, min_ratio, min_table_rows, schemas)
        issues: list[HotUpdateIssue] = []
        for r in rows:
            data = dict(r)
            data["fillfactor_warning"] = data["fillfactor"] == 100
            issues.append(HotUpdateIssue(**data))
        return issues

    async def _audit_redundant_indexes(
        self,
        conn: asyncpg.Connection,
        schemas: list[str],
        min_size_bytes: int,
    ) -> list[RedundantIndexIssue]:
        rows = await conn.fetch(SQL_REDUNDANT_ASYNCPG, schemas, min_size_bytes)
        return [RedundantIndexIssue(**dict(r)) for r in rows]

    async def _audit_low_usage_indexes(
        self,
        conn: asyncpg.Connection,
        schemas: list[str],
        min_size_bytes: int,
        min_table_rows: int,
        max_rw_ratio: float = 0.05,
    ) -> list[LowUsageIndexIssue]:
        rows = await conn.fetch(
            SQL_LOW_USAGE_ASYNCPG, schemas, max_rw_ratio, min_size_bytes, min_table_rows
        )
        return [LowUsageIndexIssue(**dict(r)) for r in rows]


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
        "broken HOT updates, redundant indexes, and low-usage/unprofitable indexes."
    ),
)
async def pg_health_audit(
    schemas: list[str] | None = None,
    enabled_checks: list[CheckName] | None = None,
    min_table_rows: int = 1000,
    min_size_bytes: int = 10_000_000,
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

    executor = AsyncPostgresHealthAuditor(
        dsn=dsn,
        connect_timeout=connect_timeout if connect_timeout is not None else 10,
    )
    report = await executor.run_full_audit(
        schemas=effective_schemas,
        checks=checks_to_run,
        min_table_rows=min_table_rows,
        min_size_bytes=min_size_bytes,
        db_alias=db_alias,
    )

    elapsed_ms = (time.perf_counter() - start_time) * 1000
    report.execution_time_ms = round(elapsed_ms, 2)
    return report


@mcp.tool(
    name="pg_locks",
    description=(
        "Inspects active lock contention and reconstructs blocking trees in PostgreSQL. "
        "Identifies root blocking sessions, blocked processes, lock wait durations, and queries."
    ),
)
async def pg_locks(
    db_alias: str = "default",
    connect_timeout: int | None = None,
) -> LockContentionReport:
    """Inspects active lock contention and blocking trees."""
    dsn = resolve_dsn(db_alias)
    auditor = AsyncPostgresHealthAuditor(
        dsn=dsn,
        connect_timeout=connect_timeout if connect_timeout is not None else 10,
    )
    return await auditor.audit_locks()


@mcp.tool(
    name="pg_bloat",
    description=(
        "Estimates physical disk bloat for PostgreSQL tables and B-tree indexes from "
        "catalog statistics. Calculates expected pages vs actual pages, wasted bytes, "
        "and bloat percentage."
    ),
)
async def pg_bloat(
    schemas: list[str] | None = None,
    min_bloat_bytes: int = 10_000_000,
    min_bloat_ratio_pct: float = 20.0,
    db_alias: str = "default",
    connect_timeout: int | None = None,
) -> BloatReport:
    """Estimates physical bloat for tables and B-tree indexes."""
    dsn = resolve_dsn(db_alias)
    auditor = AsyncPostgresHealthAuditor(
        dsn=dsn,
        connect_timeout=connect_timeout if connect_timeout is not None else 10,
    )
    return await auditor.audit_bloat(
        schemas=schemas,
        min_bloat_bytes=min_bloat_bytes,
        min_bloat_ratio_pct=min_bloat_ratio_pct,
        db_alias=db_alias,
    )


@mcp.tool(
    name="pg_explain",
    description=(
        "Simulates or executes a SQL query using PostgreSQL EXPLAIN (or safe transactional "
        "EXPLAIN ANALYZE with immediate rollback) and audits the plan for performance bottlenecks: "
        "large sequential scans, work_mem disk spills, cardinality estimation skew, and "
        "high buffer I/O."
    ),
)
async def pg_explain(
    query: str,
    analyze: bool = False,
    db_alias: str = "default",
    connect_timeout: int | None = None,
) -> PlanAnalysisReport:
    """Simulates or audits a PostgreSQL execution plan."""
    dsn = resolve_dsn(db_alias)
    auditor = AsyncPostgresHealthAuditor(
        dsn=dsn,
        connect_timeout=connect_timeout if connect_timeout is not None else 10,
    )
    return await auditor.explain_query(query=query, analyze=analyze, db_alias=db_alias)


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
        default=1000,
        help="Min live rows in table to evaluate (default: 1000)",
    )
    parser.add_argument(
        "--min-size-bytes",
        type=int,
        default=10_000_000,
        help="Min index size in bytes (default: 10MB)",
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
    parser.add_argument(
        "--analyze", action="store_true", help="Execute query in rolled-back tx for runtime stats"
    )

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
            analyze=args.analyze,
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
