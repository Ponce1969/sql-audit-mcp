"""Synchronous PostgreSQL health auditor using psycopg2."""

from typing import Any
from urllib.parse import urlsplit

from sql_audit.application.bloat_service import build_bloat_report
from sql_audit.application.lock_service import build_lock_contention_report
from sql_audit.application.plan_service import (
    analyze_execution_plan,
    validate_explain_query,
)
from sql_audit.domain.bloat import BloatReport
from sql_audit.domain.issues import (
    ALL_CHECKS,
    DatabaseHealthReport,
    DeadTuplesIssue,
    HotUpdateIssue,
    InvalidIndexIssue,
    LowUsageIndexIssue,
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
    SQL_DEAD_TUPLES_ALL_PSYCOPG,
    SQL_DEAD_TUPLES_SCHEMAS_PSYCOPG,
    SQL_HOT_PSYCOPG,
    SQL_INDEX_BLOAT_ALL_PSYCOPG,
    SQL_INDEX_BLOAT_SCHEMAS_PSYCOPG,
    SQL_INVALID_INDEXES_ALL_PSYCOPG,
    SQL_INVALID_INDEXES_SCHEMAS_PSYCOPG,
    SQL_LOCK_CONTENTION,
    SQL_LOW_USAGE_PSYCOPG,
    SQL_REDUNDANT_PSYCOPG,
    SQL_TABLE_BLOAT_ALL_PSYCOPG,
    SQL_TABLE_BLOAT_SCHEMAS_PSYCOPG,
    SQL_UNINDEXED_FKS_ALL_PSYCOPG,
    SQL_UNINDEXED_FKS_SCHEMAS_PSYCOPG,
)

try:
    import psycopg2
    import psycopg2.extras

    HAS_PSYCOPG2 = True
except ImportError:  # pragma: no cover
    HAS_PSYCOPG2 = False

DEFAULT_CONNECT_TIMEOUT = 10


def redact_db_url(db_url: str) -> str:
    """Return only the host portion of a connection URL (never credentials)."""
    try:
        parts = urlsplit(db_url)
        host = parts.hostname
        return host if host else "localhost"
    except ValueError:
        return "localhost"


class DatabaseConnectionError(Exception):
    """Raised when the database connection cannot be established."""


class PostgresHealthAuditor:
    """Audits PostgreSQL catalog and runtime statistics for indexing and storage anti-patterns."""

    def __init__(
        self,
        db_url: str,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    ):
        self.db_url = db_url
        self.connect_timeout = connect_timeout
        self.statement_timeout_ms = statement_timeout_ms
        self.lock_timeout_ms = lock_timeout_ms

    def _connect(self) -> Any:
        import audit_pg

        if not getattr(audit_pg, "HAS_PSYCOPG2", HAS_PSYCOPG2):
            raise RuntimeError("psycopg2 is not installed. Run: uv add psycopg2-binary")

        try:
            options = (
                f"-c statement_timeout={self.statement_timeout_ms} "
                f"-c lock_timeout={self.lock_timeout_ms}"
            )
            pg_mod = getattr(audit_pg, "psycopg2", psycopg2)
            if pg_mod is None:
                raise RuntimeError("psycopg2 is not installed. Run: uv add psycopg2-binary")
            return pg_mod.connect(
                self.db_url,
                connect_timeout=self.connect_timeout,
                options=options,
            )
        except Exception as exc:  # noqa: BLE001
            raise DatabaseConnectionError(str(exc)) from exc

    def run_audit(
        self,
        min_hot_ratio_pct: float = 30.0,
        min_updates_threshold: int = 50,
        max_rw_ratio: float = 0.05,
        checks: list[str] | None = None,
        schemas: list[str] | None = None,
        min_size_bytes: int = DEFAULT_MIN_SIZE_BYTES,
        min_table_rows: int = DEFAULT_MIN_TABLE_ROWS,
    ) -> DatabaseHealthReport:
        conn = self._connect()

        selected = checks if checks is not None else ALL_CHECKS
        executed_checks: list[str] = []
        failed_checks: list[str] = []
        audit_errors: list[str] = []

        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                hot_issues: list[HotUpdateIssue] = []
                redundant_indexes: list[RedundantIndexIssue] = []
                low_usage: list[LowUsageIndexIssue] = []
                invalid_indexes: list[InvalidIndexIssue] = []
                unindexed_fks: list[UnindexedFKIssue] = []
                autovacuum_dead_tuples: list[DeadTuplesIssue] = []

                if "redundant" in selected:
                    try:
                        redundant_indexes = self._audit_redundant_indexes(
                            cur, schemas, min_size_bytes
                        )
                        executed_checks.append("redundant")
                    except Exception as exc:  # noqa: BLE001
                        conn.rollback()
                        failed_checks.append("redundant")
                        audit_errors.append(f"redundant: {exc}")

                if "hot" in selected:
                    try:
                        hot_issues = self._audit_hot_and_fillfactor(
                            cur,
                            min_hot_ratio_pct,
                            min_updates_threshold,
                            schemas,
                            min_table_rows,
                        )
                        executed_checks.append("hot")
                    except Exception as exc:  # noqa: BLE001
                        conn.rollback()
                        failed_checks.append("hot")
                        audit_errors.append(f"hot: {exc}")

                if "low-usage" in selected:
                    try:
                        low_usage = self._audit_low_usage_indexes(
                            cur,
                            max_rw_ratio,
                            schemas,
                            min_size_bytes,
                            min_table_rows,
                        )
                        executed_checks.append("low-usage")
                    except Exception as exc:  # noqa: BLE001
                        conn.rollback()
                        failed_checks.append("low-usage")
                        audit_errors.append(f"low-usage: {exc}")

                if "invalid" in selected:
                    try:
                        invalid_indexes = self._audit_invalid_indexes(cur, schemas)
                        executed_checks.append("invalid")
                    except Exception as exc:  # noqa: BLE001
                        conn.rollback()
                        failed_checks.append("invalid")
                        audit_errors.append(f"invalid: {exc}")

                if "unindexed-fks" in selected:
                    try:
                        unindexed_fks = self._audit_unindexed_fks(cur, schemas)
                        executed_checks.append("unindexed-fks")
                    except Exception as exc:  # noqa: BLE001
                        conn.rollback()
                        failed_checks.append("unindexed-fks")
                        audit_errors.append(f"unindexed-fks: {exc}")

                if "dead-tuples" in selected:
                    try:
                        autovacuum_dead_tuples = self._audit_autovacuum_dead_tuples(cur, schemas)
                        executed_checks.append("dead-tuples")
                    except Exception as exc:  # noqa: BLE001
                        conn.rollback()
                        failed_checks.append("dead-tuples")
                        audit_errors.append(f"dead-tuples: {exc}")

                is_partial = bool(failed_checks or audit_errors)

                return DatabaseHealthReport(
                    hot_issues=hot_issues,
                    redundant_indexes=redundant_indexes,
                    low_usage_indexes=low_usage,
                    invalid_indexes=invalid_indexes,
                    unindexed_fks=unindexed_fks,
                    autovacuum_dead_tuples=autovacuum_dead_tuples,
                    checks_executed=executed_checks,
                    checks_failed=failed_checks,
                    errors=audit_errors,
                    is_partial=is_partial,
                )
        finally:
            conn.close()

    def audit_locks(self) -> LockContentionReport:
        """Inspects active lock contention and reconstructs blocking trees."""
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(SQL_LOCK_CONTENTION)
                rows = [dict(r) for r in cur.fetchall()]
                return build_lock_contention_report(rows)
        finally:
            conn.close()

    def audit_bloat(
        self,
        schemas: list[str] | None = None,
        min_bloat_bytes: int = 10_000_000,
        min_bloat_ratio_pct: float = 20.0,
    ) -> BloatReport:
        """Estimates physical bloat for tables and B-tree indexes from catalog statistics."""
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if schemas:
                    cur.execute(SQL_TABLE_BLOAT_SCHEMAS_PSYCOPG, [schemas])
                    tbl_rows = [dict(r) for r in cur.fetchall()]
                    cur.execute(SQL_INDEX_BLOAT_SCHEMAS_PSYCOPG, [schemas])
                    idx_rows = [dict(r) for r in cur.fetchall()]
                else:
                    cur.execute(SQL_TABLE_BLOAT_ALL_PSYCOPG)
                    tbl_rows = [dict(r) for r in cur.fetchall()]
                    cur.execute(SQL_INDEX_BLOAT_ALL_PSYCOPG)
                    idx_rows = [dict(r) for r in cur.fetchall()]

                return build_bloat_report(
                    table_rows=tbl_rows,
                    index_rows=idx_rows,
                    database=redact_db_url(self.db_url),
                    min_bloat_bytes=min_bloat_bytes,
                    min_bloat_ratio_pct=min_bloat_ratio_pct,
                )
        finally:
            conn.close()

    def explain_query(
        self,
        query: str,
    ) -> PlanAnalysisReport:
        """Runs static EXPLAIN and audits the estimated execution plan."""
        validated_query = validate_explain_query(query)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                explain_sql = f"EXPLAIN (COSTS, VERBOSE, FORMAT JSON) {validated_query}"
                cur.execute(explain_sql)
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("EXPLAIN did not return plan data")
                plan_data = row[0]

                return analyze_execution_plan(
                    raw_plan_data=plan_data,
                    query=validated_query,
                    database=redact_db_url(self.db_url),
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

    def _audit_unindexed_fks(self, cur: Any, schemas: list[str] | None) -> list[UnindexedFKIssue]:
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


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "HAS_PSYCOPG2",
    "DatabaseConnectionError",
    "PostgresHealthAuditor",
    "redact_db_url",
]
