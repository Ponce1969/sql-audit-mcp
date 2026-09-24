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
from typing import Any

import asyncpg
from dotenv import find_dotenv, load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

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
                hot_issues = await self._audit_hot_and_fillfactor(
                    conn, schemas, min_table_rows
                )
            if CheckName.REDUNDANT_INDEXES.value in checks:
                redundant = await self._audit_redundant_indexes(
                    conn, schemas, min_size_bytes
                )
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

    async def _audit_invalid_indexes(
        self, conn: asyncpg.Connection, schemas: list[str]
    ) -> list[InvalidIndexIssue]:
        query = """
        SELECT
            c.relname AS child_table,
            idx.relname AS invalid_index,
            pg_size_pretty(pg_relation_size(i.indexrelid)) AS index_size
        FROM pg_index i
        JOIN pg_class idx ON idx.oid = i.indexrelid
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE i.indisvalid = FALSE
          AND n.nspname = ANY($1)
        ORDER BY pg_relation_size(i.indexrelid) DESC;
        """
        rows = await conn.fetch(query, schemas)
        return [InvalidIndexIssue(**dict(r)) for r in rows]

    async def _audit_unindexed_fks(
        self, conn: asyncpg.Connection, schemas: list[str]
    ) -> list[UnindexedFKIssue]:
        query = """
        SELECT
            c.conrelid::regclass::text AS child_table,
            c.conname AS fk_name,
            c.confrelid::regclass::text AS parent_table,
            pg_get_constraintdef(c.oid) AS fk_definition
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE c.contype = 'f'
          AND n.nspname = ANY($1)
          AND NOT EXISTS (
            SELECT 1
            FROM pg_index i
            WHERE i.indrelid = c.conrelid
              AND (string_to_array(i.indkey::text, ' '))[1:cardinality(c.conkey)] =
                  string_to_array(array_to_string(c.conkey, ' '), ' ')
              AND i.indisvalid
          )
        ORDER BY c.conrelid::regclass::text, c.conname;
        """
        rows = await conn.fetch(query, schemas)
        return [UnindexedFKIssue(**dict(r)) for r in rows]

    async def _audit_autovacuum_dead_tuples(
        self, conn: asyncpg.Connection, schemas: list[str]
    ) -> list[DeadTuplesIssue]:
        query = """
        SELECT
            st.relname AS table_name,
            st.n_dead_tup AS dead_tuples,
            st.n_live_tup AS live_tuples,
            ROUND((st.n_dead_tup::numeric / NULLIF(st.n_live_tup + st.n_dead_tup, 0)) * 100, 2)::float AS dead_tuple_pct,
            st.last_autovacuum,
            st.last_vacuum
        FROM pg_stat_user_tables st
        WHERE st.schemaname = ANY($1)
          AND st.n_dead_tup > 10000
          AND (st.n_dead_tup::numeric / NULLIF(st.n_live_tup + st.n_dead_tup, 0)) > 0.15
        ORDER BY st.n_dead_tup DESC;
        """
        rows = await conn.fetch(query, schemas)
        return [DeadTuplesIssue(**dict(r)) for r in rows]

    async def _audit_hot_and_fillfactor(
        self,
        conn: asyncpg.Connection,
        schemas: list[str],
        min_table_rows: int,
        min_ratio: float = 30.0,
        min_updates: int = 50,
    ) -> list[HotUpdateIssue]:
        query = """
        SELECT
            t.relname AS table_name,
            t.n_tup_upd AS total_updates,
            t.n_tup_hot_upd AS hot_updates,
            ROUND((t.n_tup_hot_upd::numeric / NULLIF(t.n_tup_upd, 0)) * 100, 2)::float AS hot_ratio_pct,
            COALESCE(
                (
                    SELECT option_value::int
                    FROM pg_options_to_table(c.reloptions)
                    WHERE option_name = 'fillfactor'
                ), 100
            ) AS fillfactor,
            t.n_live_tup AS table_rows,
            pg_size_pretty(pg_total_relation_size(t.relid)) AS table_size
        FROM pg_stat_user_tables t
        JOIN pg_class c ON c.oid = t.relid
        JOIN pg_namespace ns ON ns.oid = c.relnamespace
        WHERE t.n_tup_upd >= $1
          AND ROUND((t.n_tup_hot_upd::numeric / NULLIF(t.n_tup_upd, 0)) * 100, 2) < $2
          AND t.n_live_tup >= $3
          AND ns.nspname = ANY($4)
        ORDER BY t.n_tup_upd DESC;
        """
        rows = await conn.fetch(query, min_updates, min_ratio, min_table_rows, schemas)
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
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_am am ON am.oid = c.relam
            WHERE am.amname = 'btree'
              AND i.indisvalid
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
        WHERE n.nspname = ANY($1)
          AND NOT p1.indisunique
          AND NOT p1.indisprimary
          AND p1.indpred IS NULL
          AND '0' != ALL(p1.keys)
          AND '0' != ALL(p2.keys)
          AND p2.keys[1:cardinality(p1.keys)] = p1.keys
          AND p1.size_bytes >= $2
        ORDER BY p1.size_bytes DESC;
        """
        rows = await conn.fetch(query, schemas, min_size_bytes)
        return [RedundantIndexIssue(**dict(r)) for r in rows]

    async def _audit_low_usage_indexes(
        self,
        conn: asyncpg.Connection,
        schemas: list[str],
        min_size_bytes: int,
        min_table_rows: int,
        max_rw_ratio: float = 0.05,
    ) -> list[LowUsageIndexIssue]:
        query = """
        SELECT
            c.relname AS table_name,
            i.relname AS index_name,
            pg_size_pretty(pg_relation_size(i.oid)) AS size,
            s.idx_scan AS index_scans,
            (t.n_tup_ins + t.n_tup_upd + t.n_tup_del) AS table_writes,
            ROUND(
                (s.idx_scan::numeric / NULLIF(t.n_tup_ins + t.n_tup_upd + t.n_tup_del, 0)),
                4
            )::float AS read_write_ratio,
            t.n_live_tup AS table_rows,
            pg_size_pretty(pg_total_relation_size(t.relid)) AS table_size
        FROM pg_stat_user_indexes s
        JOIN pg_stat_user_tables t ON t.relid = s.relid
        JOIN pg_class c ON c.oid = s.relid
        JOIN pg_class i ON i.oid = s.indexrelid
        JOIN pg_index ix ON ix.indexrelid = s.indexrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY($1)
          AND NOT ix.indisunique
          AND NOT ix.indisprimary
          AND (t.n_tup_ins + t.n_tup_upd + t.n_tup_del) > 1000
          AND (s.idx_scan::numeric / NULLIF(t.n_tup_ins + t.n_tup_upd + t.n_tup_del, 0)) <= $2
          AND pg_relation_size(i.oid) >= $3
          AND t.n_live_tup >= $4
        ORDER BY pg_relation_size(i.oid) DESC;
        """
        rows = await conn.fetch(
            query, schemas, max_rw_ratio, min_size_bytes, min_table_rows
        )
        return [LowUsageIndexIssue(**dict(r)) for r in rows]


# --- FastMCP Server Definition ---

mcp = FastMCP("PostgreSQL Health Auditor")


def resolve_dsn(db_alias: str) -> str:
    """Resolves DSN from environment without exposing credentials to MCP protocol."""
    env_var = (
        f"DB_{db_alias.upper()}_URL" if db_alias.lower() != "default" else "DATABASE_URL"
    )
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
    schemas: list[str] = ["public"],
    enabled_checks: list[CheckName] | None = None,
    min_table_rows: int = 1000,
    min_size_bytes: int = 10_000_000,
    db_alias: str = "default",
) -> PostgresHealthReport:
    """Deterministic MCP tool with flat parameters and environment-backed DSN resolution."""
    start_time = time.perf_counter()
    dsn = resolve_dsn(db_alias)

    checks_to_run = (
        [c.value for c in enabled_checks]
        if enabled_checks is not None
        else [c.value for c in CheckName]
    )

    executor = AsyncPostgresHealthAuditor(dsn=dsn)
    report = await executor.run_full_audit(
        schemas=schemas,
        checks=checks_to_run,
        min_table_rows=min_table_rows,
        min_size_bytes=min_size_bytes,
        db_alias=db_alias,
    )

    elapsed_ms = (time.perf_counter() - start_time) * 1000
    report.execution_time_ms = round(elapsed_ms, 2)
    return report


async def _run_cli_main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Deterministic PostgreSQL Health Auditor (CLI mode)"
    )
    parser.add_argument(
        "--dsn", default=None, help="PostgreSQL DSN (or via DATABASE_URL)"
    )
    parser.add_argument(
        "--alias", default="default", help="Database alias for DB_{ALIAS}_URL"
    )
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
        "--json", action="store_true", help="Output full report as formatted JSON"
    )

    raw_args = [a for a in sys.argv[1:] if a not in ("--cli", "-c")]
    args = parser.parse_args(raw_args)

    dsn = args.dsn or os.getenv("DATABASE_URL")
    if not dsn:
        dsn = resolve_dsn(args.alias)

    schemas = [s.strip() for s in args.schemas.split(",") if s.strip()]
    auditor = AsyncPostgresHealthAuditor(dsn=dsn)

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
    print(f"\n=======================================================")
    print(f" PostgreSQL Health Audit Report ({report.database_alias})")
    print(
        f" Execution time: {report.execution_time_ms} ms | Schemas: {', '.join(report.schemas_audited)}"
    )
    print(f" Critical issues detected: {'YES' if report.has_critical_issues else 'NO'}")
    print(f"=======================================================\n")

    print(f"[1] Invalid Indexes (indisvalid = false): {len(report.invalid_indexes)}")
    for item in report.invalid_indexes:
        print(
            f"    - Table: {item.child_table} | Index: {item.invalid_index} (Size: {item.index_size})"
        )

    print(f"[2] Unindexed Foreign Keys: {len(report.unindexed_fks)}")
    for item in report.unindexed_fks:
        print(f"    - FK: {item.fk_name} on {item.child_table} -> {item.parent_table}")
        print(f"      Def: {item.fk_definition}")

    print(f"[3] Autovacuum & Dead Tuples Lag: {len(report.autovacuum_dead_tuples)}")
    for item in report.autovacuum_dead_tuples:
        print(
            f"    - Table: {item.table_name} | Dead tuples: {item.dead_tuples:,} ({item.dead_tuple_pct}%) | Live: {item.live_tuples:,}"
        )

    print(
        f"[4] Broken HOT Updates / Fillfactor Issues: {len(report.hot_fillfactor_issues)}"
    )
    for item in report.hot_fillfactor_issues:
        print(
            f"    - Table: {item.table_name} | HOT ratio: {item.hot_ratio_pct}% ({item.hot_updates}/{item.total_updates}) | Fillfactor: {item.fillfactor}"
        )

    print(f"[5] Redundant / Duplicate Indexes: {len(report.redundant_indexes)}")
    for item in report.redundant_indexes:
        print(
            f"    - Table: {item.table_name} | Redundant: {item.redundant_index} ({item.redundant_size}) covered by {item.covering_index}"
        )

    print(f"[6] Low Usage / Unprofitable Indexes: {len(report.low_usage_indexes)}")
    for item in report.low_usage_indexes:
        print(
            f"    - Table: {item.table_name} | Index: {item.index_name} ({item.size}) | Scans: {item.index_scans} vs Writes: {item.table_writes} (Ratio: {item.read_write_ratio})"
        )

    print("\nAudit completed.\n")


if __name__ == "__main__":
    if "--cli" in sys.argv or "-c" in sys.argv:
        import asyncio

        asyncio.run(_run_cli_main())
    else:
        mcp.run(transport="stdio")

