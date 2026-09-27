"""Tests for the canonical contract (AuditReport, Finding, Evidence) across CLI and MCP."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import audit_pg
from audit_pg import (
    DatabaseHealthReport,
)
from audit_pg import (
    DeadTuplesIssue as CliDeadTuples,
)
from audit_pg import (
    HotUpdateIssue as CliHot,
)
from audit_pg import (
    InvalidIndexIssue as CliInvalid,
)
from audit_pg import (
    LowUsageIndexIssue as CliLowUsage,
)
from audit_pg import (
    RedundantIndexIssue as CliRedundant,
)
from audit_pg import (
    UnindexedFKIssue as CliUnindexedFK,
)
from mcp_pg_auditor import (
    DeadTuplesIssue as McpDeadTuples,
)
from mcp_pg_auditor import (
    HotUpdateIssue as McpHot,
)
from mcp_pg_auditor import (
    InvalidIndexIssue as McpInvalid,
)
from mcp_pg_auditor import (
    LowUsageIndexIssue as McpLowUsage,
)
from mcp_pg_auditor import (
    PostgresHealthReport,
)
from mcp_pg_auditor import (
    RedundantIndexIssue as McpRedundant,
)
from mcp_pg_auditor import (
    UnindexedFKIssue as McpUnindexedFK,
)


def test_cli_and_mcp_produce_equivalent_audit_reports():
    """Demonstrates equivalence between CLI and MCP report translation to AuditReport."""
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Build CLI report
    cli_report = DatabaseHealthReport(
        hot_issues=[CliHot("users", 1000, 150, 15.0, 100, True, 25000, "18 MB")],
        redundant_indexes=[
            CliRedundant(
                "users",
                "idx_users_email",
                "16 MB",
                "idx_users_email_created",
                "def a",
                "def b",
            )
        ],
        low_usage_indexes=[
            CliLowUsage("orders", "idx_orders_status", "8 MB", 5, 2000, 0.0025, 50000, "45 MB")
        ],
        invalid_indexes=[CliInvalid("orders", "idx_orders_broken", "24 MB")],
        unindexed_fks=[CliUnindexedFK("order_items", "fk_items_prod", "products", "def fk")],
        autovacuum_dead_tuples=[CliDeadTuples("events", 15000, 50000, 23.08, now, None)],
    )

    # 2. Build equivalent MCP report
    mcp_report = PostgresHealthReport(
        database_alias="test_db",
        schemas_audited=["public"],
        execution_time_ms=12.5,
        invalid_indexes=[
            McpInvalid(
                child_table="orders",
                invalid_index="idx_orders_broken",
                index_size="24 MB",
            )
        ],
        unindexed_fks=[
            McpUnindexedFK(
                child_table="order_items",
                fk_name="fk_items_prod",
                parent_table="products",
                fk_definition="def fk",
            )
        ],
        autovacuum_dead_tuples=[
            McpDeadTuples(
                table_name="events",
                dead_tuples=15000,
                live_tuples=50000,
                dead_tuple_pct=23.08,
                last_autovacuum=now,
                last_vacuum=None,
            )
        ],
        hot_fillfactor_issues=[
            McpHot(
                table_name="users",
                total_updates=1000,
                hot_updates=150,
                hot_ratio_pct=15.0,
                fillfactor=100,
                fillfactor_warning=True,
                table_rows=25000,
                table_size="18 MB",
            )
        ],
        redundant_indexes=[
            McpRedundant(
                table_name="users",
                redundant_index="idx_users_email",
                redundant_size="16 MB",
                covering_index="idx_users_email_created",
                redundant_def="def a",
                covering_def="def b",
            )
        ],
        low_usage_indexes=[
            McpLowUsage(
                table_name="orders",
                index_name="idx_orders_status",
                size="8 MB",
                index_scans=5,
                table_writes=2000,
                read_write_ratio=0.0025,
                table_rows=50000,
                table_size="45 MB",
            )
        ],
    )

    cli_canonical = cli_report.to_audit_report(
        database="test_db",
        checks=["hot", "redundant", "low-usage", "invalid", "unindexed-fks", "dead-tuples"],
        observed_at=now,
        audit_id="audit_fixed_id",
    )

    mcp_canonical = mcp_report.to_audit_report(
        checks=["hot", "redundant", "low-usage", "invalid", "unindexed-fks", "dead-tuples"],
        observed_at=now,
        audit_id="audit_fixed_id",
    )

    # Validate findings count
    assert len(cli_canonical.findings) == 6
    assert len(mcp_canonical.findings) == 6

    # Validate finding_ids and evidence_ids match exactly between both adapters
    cli_findings = {f.finding_id: f for f in cli_canonical.findings}
    mcp_findings = {f.finding_id: f for f in mcp_canonical.findings}

    assert set(cli_findings.keys()) == set(mcp_findings.keys())

    for fid in cli_findings:
        cf = cli_findings[fid]
        mf = mcp_findings[fid]
        assert cf.severity == mf.severity
        assert cf.object_name == mf.object_name
        assert cf.evidence[0].evidence_id == mf.evidence[0].evidence_id


def test_cli_canonical_json_flag(capsys):
    """--canonical-json CLI flag emits valid AuditReport schema."""
    report = DatabaseHealthReport(
        invalid_indexes=[CliInvalid("orders", "idx_orders_broken", "12 MB")]
    )

    def factory(db_url, connect_timeout=10):
        mock = MagicMock()
        mock.run_audit.return_value = report
        return mock

    exit_code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/orders_db", "--canonical-json"],
        auditor_factory=factory,
    )
    assert exit_code == 3  # Critical issue detected
    captured = capsys.readouterr().out
    data = json.loads(captured)

    assert data["schema_version"] == "1.0.0"
    assert data["database"] == "localhost"
    assert data["has_critical_issues"] is True
    assert len(data["findings"]) == 1
    finding = data["findings"][0]
    assert finding["finding_id"] == "PG-INDEX-INVALID:orders:idx_orders_broken"
    assert finding["severity"] == "critical"
    assert finding["evidence"][0]["evidence_id"].startswith("sha256:")


def test_cli_and_mcp_share_canonical_defaults():
    """CLI and MCP must share the exact same canonical audit defaults (T-02)."""
    import inspect

    import mcp_pg_auditor
    from sql_audit.domain import (
        DEFAULT_MIN_SIZE_BYTES,
        DEFAULT_MIN_TABLE_ROWS,
        ExecutionMetadata,
    )

    # 1. Domain model defaults
    meta = ExecutionMetadata()
    assert meta.min_size_bytes == DEFAULT_MIN_SIZE_BYTES == 0
    assert meta.min_table_rows == DEFAULT_MIN_TABLE_ROWS == 10000

    # 2. CLI build_parser defaults
    cli_parser = audit_pg.build_parser()
    cli_defaults = cli_parser.parse_args([])
    assert cli_defaults.min_size == DEFAULT_MIN_SIZE_BYTES
    assert cli_defaults.min_table_rows == DEFAULT_MIN_TABLE_ROWS

    # 3. CLI PostgresHealthAuditor.run_audit signature defaults
    cli_sig = inspect.signature(audit_pg.PostgresHealthAuditor.run_audit)
    assert cli_sig.parameters["min_size_bytes"].default == DEFAULT_MIN_SIZE_BYTES
    assert cli_sig.parameters["min_table_rows"].default == DEFAULT_MIN_TABLE_ROWS

    # 4. MCP pg_health_audit tool signature defaults
    mcp_tool_sig = inspect.signature(mcp_pg_auditor.pg_health_audit)
    assert mcp_tool_sig.parameters["min_size_bytes"].default == DEFAULT_MIN_SIZE_BYTES
    assert mcp_tool_sig.parameters["min_table_rows"].default == DEFAULT_MIN_TABLE_ROWS

    # 5. MCP AsyncPostgresHealthAuditor.run_full_audit signature defaults
    mcp_full_sig = inspect.signature(mcp_pg_auditor.AsyncPostgresHealthAuditor.run_full_audit)
    assert mcp_full_sig.parameters["min_size_bytes"].default == DEFAULT_MIN_SIZE_BYTES
    assert mcp_full_sig.parameters["min_table_rows"].default == DEFAULT_MIN_TABLE_ROWS

    # 6. Report to_audit_report defaults
    cli_to_sig = inspect.signature(audit_pg.DatabaseHealthReport.to_audit_report)
    assert cli_to_sig.parameters["min_size_bytes"].default == DEFAULT_MIN_SIZE_BYTES
    assert cli_to_sig.parameters["min_table_rows"].default == DEFAULT_MIN_TABLE_ROWS

    mcp_to_sig = inspect.signature(mcp_pg_auditor.PostgresHealthReport.to_audit_report)
    assert mcp_to_sig.parameters["min_size_bytes"].default == DEFAULT_MIN_SIZE_BYTES
    assert mcp_to_sig.parameters["min_table_rows"].default == DEFAULT_MIN_TABLE_ROWS


async def test_default_scope_equivalence_simulated_queries(monkeypatch):
    """Running CLI and MCP with defaults queries identical scope parameters in SQL."""
    from unittest.mock import AsyncMock

    import mcp_pg_auditor

    # 1. Capture CLI query params
    cli_executed = []

    class MockCursor:
        def execute(self, sql, params=None):
            cli_executed.append((sql, params))

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class MockConn:
        def cursor(self, *args, **kwargs):
            return MockCursor()

        def close(self):
            pass

    monkeypatch.setattr(audit_pg.psycopg2, "connect", MagicMock(return_value=MockConn()))
    monkeypatch.setattr(audit_pg, "HAS_PSYCOPG2", True)

    cli_auditor = audit_pg.PostgresHealthAuditor(db_url="postgresql://u:p@localhost:5432/db")
    cli_auditor.run_audit(checks=["hot", "redundant", "low-usage"])

    # 2. Capture MCP query params
    mcp_executed = []

    mock_async_conn = AsyncMock()

    async def mock_fetch(sql, *args):
        mcp_executed.append((sql, list(args)))
        return []

    mock_async_conn.fetch = mock_fetch
    monkeypatch.setattr(mcp_pg_auditor.asyncpg, "connect", AsyncMock(return_value=mock_async_conn))

    mcp_auditor = mcp_pg_auditor.AsyncPostgresHealthAuditor(dsn="postgresql://u:p@localhost:5432/db")
    await mcp_auditor.run_full_audit(
        schemas=["public"],
        checks=["hot_fillfactor", "redundant_indexes", "low_usage_indexes"],
    )

    # Assert redundant index min_size_bytes threshold is 0 in both
    cli_redundant_params = next(p for s, p in cli_executed if "parsed_indexes" in s)
    assert cli_redundant_params[0] == 0

    mcp_redundant_args = next(args for s, args in mcp_executed if "parsed_indexes" in s)
    assert mcp_redundant_args[1] == 0  # args: [schemas, min_size_bytes]

    # Assert HOT updates min_table_rows threshold is 10000 in both
    cli_hot_params = next(p for s, p in cli_executed if "n_tup_hot_upd" in s)
    assert cli_hot_params[2] == 10000  # [min_updates, min_ratio, min_table_rows]

    mcp_hot_args = next(args for s, args in mcp_executed if "n_tup_hot_upd" in s)
    assert mcp_hot_args[2] == 10000  # [min_updates, min_ratio, min_table_rows, schemas]

    # Assert low-usage min_size_bytes (0) and min_table_rows (10000) match in both
    cli_low_usage_params = next(p for s, p in cli_executed if "idx_scan" in s)
    assert cli_low_usage_params[1] == 0
    assert cli_low_usage_params[2] == 10000

    mcp_low_usage_args = next(args for s, args in mcp_executed if "idx_scan" in s)
    assert mcp_low_usage_args[2] == 0  # [schemas, max_rw_ratio, min_size_bytes, min_table_rows]
    assert mcp_low_usage_args[3] == 10000


def test_identical_explicit_configuration_produces_identical_canonical_audit_report():
    """Identical explicit configuration produces equivalent canonical reports and metadata."""
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)

    cli_rep = DatabaseHealthReport()
    mcp_rep = PostgresHealthReport(database_alias="prod_db", schemas_audited=["public"])

    cli_canonical = cli_rep.to_audit_report(
        database="prod_db",
        observed_at=now,
        min_size_bytes=50_000,
        min_table_rows=5000,
    )
    mcp_canonical = mcp_rep.to_audit_report(
        database="prod_db",
        observed_at=now,
        min_size_bytes=50_000,
        min_table_rows=5000,
    )

    cli_meta = cli_canonical.execution_metadata
    mcp_meta = mcp_canonical.execution_metadata
    assert cli_meta.min_size_bytes == mcp_meta.min_size_bytes == 50_000
    assert cli_meta.min_table_rows == mcp_meta.min_table_rows == 5000
    assert cli_canonical.findings == mcp_canonical.findings == []

