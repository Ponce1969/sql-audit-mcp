"""Asynchronous PostgreSQL health auditor using asyncpg."""

import json
from typing import Any

import asyncpg

from sql_audit.application.bloat_service import build_bloat_report
from sql_audit.application.lock_service import build_lock_contention_report
from sql_audit.application.plan_service import (
    analyze_execution_plan,
    validate_explain_query,
)
from sql_audit.domain.bloat import BloatReport
from sql_audit.domain.issues import (
    CheckName,
    DeadTuplesIssue,
    HotUpdateIssue,
    InvalidIndexIssue,
    LowUsageIndexIssue,
    PostgresHealthReport,
    RedundantIndexIssue,
    UnindexedFKIssue,
)
from sql_audit.domain.locks import LockContentionReport
from sql_audit.domain.models import (
    DEFAULT_LOCK_TIMEOUT_MS,
    DEFAULT_MIN_SIZE_BYTES,
    DEFAULT_MIN_TABLE_ROWS,
    DEFAULT_STATEMENT_TIMEOUT_MS,
)
from sql_audit.domain.plan import PlanAnalysisReport
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


class AsyncPostgresHealthAuditor:
    """Performs deterministic catalog audits for PostgreSQL anti-patterns."""

    def __init__(
        self,
        dsn: str,
        connect_timeout: int = 10,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.statement_timeout_ms = statement_timeout_ms
        self.lock_timeout_ms = lock_timeout_ms

    async def _connect(self) -> asyncpg.Connection:
        server_settings = {
            "statement_timeout": str(self.statement_timeout_ms),
            "lock_timeout": str(self.lock_timeout_ms),
        }
        import mcp_pg_auditor

        apg = getattr(mcp_pg_auditor, "asyncpg", asyncpg)
        return await apg.connect(
            self.dsn,
            timeout=self.connect_timeout,
            server_settings=server_settings,
        )

    async def run_full_audit(
        self,
        schemas: list[str],
        checks: list[str],
        min_table_rows: int = DEFAULT_MIN_TABLE_ROWS,
        min_size_bytes: int = DEFAULT_MIN_SIZE_BYTES,
        db_alias: str = "default",
    ) -> PostgresHealthReport:
        conn = await self._connect()
        try:
            invalid_indexes: list[InvalidIndexIssue] = []
            unindexed_fks: list[UnindexedFKIssue] = []
            dead_tuples: list[DeadTuplesIssue] = []
            hot_issues: list[HotUpdateIssue] = []
            redundant: list[RedundantIndexIssue] = []
            low_usage: list[LowUsageIndexIssue] = []

            executed_checks: list[str] = []
            failed_checks: list[str] = []
            audit_errors: list[str] = []

            if CheckName.INVALID_INDEXES.value in checks:
                try:
                    invalid_indexes = await self._audit_invalid_indexes(conn, schemas)
                    executed_checks.append(CheckName.INVALID_INDEXES.value)
                except Exception as exc:  # noqa: BLE001
                    failed_checks.append(CheckName.INVALID_INDEXES.value)
                    audit_errors.append(f"{CheckName.INVALID_INDEXES.value}: {exc}")

            if CheckName.UNINDEXED_FKS.value in checks:
                try:
                    unindexed_fks = await self._audit_unindexed_fks(conn, schemas)
                    executed_checks.append(CheckName.UNINDEXED_FKS.value)
                except Exception as exc:  # noqa: BLE001
                    failed_checks.append(CheckName.UNINDEXED_FKS.value)
                    audit_errors.append(f"{CheckName.UNINDEXED_FKS.value}: {exc}")

            if CheckName.AUTOVACUUM_DEAD_TUPLES.value in checks:
                try:
                    dead_tuples = await self._audit_autovacuum_dead_tuples(conn, schemas)
                    executed_checks.append(CheckName.AUTOVACUUM_DEAD_TUPLES.value)
                except Exception as exc:  # noqa: BLE001
                    failed_checks.append(CheckName.AUTOVACUUM_DEAD_TUPLES.value)
                    audit_errors.append(f"{CheckName.AUTOVACUUM_DEAD_TUPLES.value}: {exc}")

            if CheckName.HOT_FILLFACTOR.value in checks:
                try:
                    hot_issues = await self._audit_hot_and_fillfactor(conn, schemas, min_table_rows)
                    executed_checks.append(CheckName.HOT_FILLFACTOR.value)
                except Exception as exc:  # noqa: BLE001
                    failed_checks.append(CheckName.HOT_FILLFACTOR.value)
                    audit_errors.append(f"{CheckName.HOT_FILLFACTOR.value}: {exc}")

            if CheckName.REDUNDANT_INDEXES.value in checks:
                try:
                    redundant = await self._audit_redundant_indexes(conn, schemas, min_size_bytes)
                    executed_checks.append(CheckName.REDUNDANT_INDEXES.value)
                except Exception as exc:  # noqa: BLE001
                    failed_checks.append(CheckName.REDUNDANT_INDEXES.value)
                    audit_errors.append(f"{CheckName.REDUNDANT_INDEXES.value}: {exc}")

            if CheckName.LOW_USAGE_INDEXES.value in checks:
                try:
                    low_usage = await self._audit_low_usage_indexes(
                        conn, schemas, min_size_bytes, min_table_rows
                    )
                    executed_checks.append(CheckName.LOW_USAGE_INDEXES.value)
                except Exception as exc:  # noqa: BLE001
                    failed_checks.append(CheckName.LOW_USAGE_INDEXES.value)
                    audit_errors.append(f"{CheckName.LOW_USAGE_INDEXES.value}: {exc}")

            has_critical = bool(invalid_indexes or unindexed_fks or dead_tuples)
            is_partial = bool(failed_checks or audit_errors)

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
                checks_executed=executed_checks,
                checks_failed=failed_checks,
                errors=audit_errors,
                is_partial=is_partial,
            )
        finally:
            await conn.close()

    async def audit_locks(self) -> LockContentionReport:
        """Inspects active lock contention and reconstructs blocking trees."""
        conn = await self._connect()
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
        conn = await self._connect()
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
        db_alias: str = "default",
    ) -> PlanAnalysisReport:
        """Runs static EXPLAIN and audits the estimated execution plan."""
        validated_query = validate_explain_query(query)
        conn = await self._connect()
        try:
            explain_sql = f"EXPLAIN (COSTS, VERBOSE, FORMAT JSON) {validated_query}"
            raw = await conn.fetchval(explain_sql)

            plan_data = json.loads(raw) if isinstance(raw, str) else raw
            return analyze_execution_plan(
                raw_plan_data=plan_data,
                query=validated_query,
                database=db_alias,
            )
        finally:
            await conn.close()

    async def _audit_invalid_indexes(
        self, conn: Any, schemas: list[str]
    ) -> list[InvalidIndexIssue]:
        rows = await conn.fetch(SQL_INVALID_INDEXES_ASYNCPG, schemas)
        return [InvalidIndexIssue(**dict(r)) for r in rows]

    async def _audit_unindexed_fks(
        self, conn: Any, schemas: list[str]
    ) -> list[UnindexedFKIssue]:
        rows = await conn.fetch(SQL_UNINDEXED_FKS_ASYNCPG, schemas)
        return [UnindexedFKIssue(**dict(r)) for r in rows]

    async def _audit_autovacuum_dead_tuples(
        self, conn: Any, schemas: list[str]
    ) -> list[DeadTuplesIssue]:
        rows = await conn.fetch(SQL_DEAD_TUPLES_ASYNCPG, schemas)
        return [DeadTuplesIssue(**dict(r)) for r in rows]

    async def _audit_hot_and_fillfactor(
        self,
        conn: Any,
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
        conn: Any,
        schemas: list[str],
        min_size_bytes: int,
    ) -> list[RedundantIndexIssue]:
        rows = await conn.fetch(SQL_REDUNDANT_ASYNCPG, schemas, min_size_bytes)
        return [RedundantIndexIssue(**dict(r)) for r in rows]

    async def _audit_low_usage_indexes(
        self,
        conn: Any,
        schemas: list[str],
        min_size_bytes: int,
        min_table_rows: int,
        max_rw_ratio: float = 0.05,
    ) -> list[LowUsageIndexIssue]:
        rows = await conn.fetch(
            SQL_LOW_USAGE_ASYNCPG, schemas, max_rw_ratio, min_size_bytes, min_table_rows
        )
        return [LowUsageIndexIssue(**dict(r)) for r in rows]


__all__ = ["AsyncPostgresHealthAuditor"]
