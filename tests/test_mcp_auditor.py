"""Tests for mcp_pg_auditor — AsyncPostgresHealthAuditor and pg_health_audit tool.

Covers:
  - resolve_dsn (3.2)
  - _audit_invalid_indexes (3.3)
  - _audit_unindexed_fks (3.4)
  - _audit_autovacuum_dead_tuples (3.5)
  - run_full_audit all checks (3.6)
  - run_full_audit partial checks (3.7)
  - has_critical_issues on PostgresHealthReport (3.8)
  - _print_cli_summary output (3.9)
  - schema list forwarding (3.10)
"""

from unittest.mock import AsyncMock, patch

import pytest

from mcp_pg_auditor import (
    AsyncPostgresHealthAuditor,
    CheckName,
    DeadTuplesIssue,
    InvalidIndexIssue,
    PostgresHealthReport,
    UnindexedFKIssue,
    _print_cli_summary,
    resolve_dsn,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeAsyncAuditor:
    """Mirrors FakeAuditor from test_cli.py — deterministic report stub."""

    def __init__(self, report: PostgresHealthReport):
        self._report = report

    async def run_full_audit(self, **kw) -> PostgresHealthReport:
        return self._report


def make_mock_conn(rows_per_call: list | None = None) -> AsyncMock:
    """Return an AsyncMock connection with configurable fetch side_effect."""
    conn = AsyncMock()
    if rows_per_call is not None:
        conn.fetch.side_effect = rows_per_call
    else:
        conn.fetch.return_value = []
    return conn


# ---------------------------------------------------------------------------
# 3.2 — resolve_dsn
# ---------------------------------------------------------------------------


def test_resolve_dsn_default_alias(monkeypatch):
    """DATABASE_URL in env → resolve_dsn("default") returns that URL."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert resolve_dsn("default") == "postgresql://u:p@h/db"


def test_resolve_dsn_custom_alias(monkeypatch):
    """DB_PROD_URL in env → resolve_dsn("prod") returns it."""
    monkeypatch.setenv("DB_PROD_URL", "postgresql://u:p@prod/db")
    assert resolve_dsn("prod") == "postgresql://u:p@prod/db"


def test_resolve_dsn_missing_raises_value_error(monkeypatch):
    """No env var set → ValueError raised containing the alias name."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_STAGING_URL", raising=False)
    with pytest.raises(ValueError, match="staging"):
        resolve_dsn("staging")


# ---------------------------------------------------------------------------
# 3.3 — _audit_invalid_indexes
# ---------------------------------------------------------------------------


async def test_audit_invalid_indexes_maps_rows():
    """One row dict from conn.fetch → one InvalidIndexIssue with correct fields."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"child_table": "orders", "invalid_index": "idx_stale", "index_size": "16 kB"}
    ]
    auditor = AsyncPostgresHealthAuditor(dsn="postgresql://u:p@h/db")
    result = await auditor._audit_invalid_indexes(mock_conn, ["public"])
    assert len(result) == 1
    issue = result[0]
    assert isinstance(issue, InvalidIndexIssue)
    assert issue.child_table == "orders"
    assert issue.invalid_index == "idx_stale"
    assert issue.index_size == "16 kB"


async def test_audit_invalid_indexes_empty_returns_empty_list():
    """conn.fetch returns [] → result is an empty list."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []
    auditor = AsyncPostgresHealthAuditor(dsn="postgresql://u:p@h/db")
    result = await auditor._audit_invalid_indexes(mock_conn, ["public"])
    assert result == []


# ---------------------------------------------------------------------------
# 3.4 — _audit_unindexed_fks
# ---------------------------------------------------------------------------


async def test_audit_unindexed_fks_maps_rows():
    """One FK row from conn.fetch → one UnindexedFKIssue with correct fields."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "child_table": "order_items",
            "fk_name": "fk_order_items_order_id",
            "parent_table": "orders",
            "fk_definition": "FOREIGN KEY (order_id) REFERENCES orders(id)",
        }
    ]
    auditor = AsyncPostgresHealthAuditor(dsn="postgresql://u:p@h/db")
    result = await auditor._audit_unindexed_fks(mock_conn, ["public"])
    assert len(result) == 1
    issue = result[0]
    assert isinstance(issue, UnindexedFKIssue)
    assert issue.child_table == "order_items"
    assert issue.fk_name == "fk_order_items_order_id"
    assert issue.parent_table == "orders"


# ---------------------------------------------------------------------------
# 3.5 — _audit_autovacuum_dead_tuples
# ---------------------------------------------------------------------------


async def test_audit_dead_tuples_maps_rows_with_null_timestamps():
    """Row with last_autovacuum=None → DeadTuplesIssue with last_autovacuum=None."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "table_name": "events",
            "dead_tuples": 50000,
            "live_tuples": 200000,
            "dead_tuple_pct": 20.0,
            "last_autovacuum": None,
            "last_vacuum": None,
        }
    ]
    auditor = AsyncPostgresHealthAuditor(dsn="postgresql://u:p@h/db")
    result = await auditor._audit_autovacuum_dead_tuples(mock_conn, ["public"])
    assert len(result) == 1
    issue = result[0]
    assert isinstance(issue, DeadTuplesIssue)
    assert issue.table_name == "events"
    assert issue.dead_tuples == 50000
    assert issue.last_autovacuum is None
    assert issue.last_vacuum is None


# ---------------------------------------------------------------------------
# 3.6 — run_full_audit with all checks enabled
# ---------------------------------------------------------------------------


async def test_run_full_audit_all_checks():
    """All checks → each list populated; has_critical_issues True (invalid_indexes non-empty)."""
    invalid_row = {
        "child_table": "tbl",
        "invalid_index": "idx_bad",
        "index_size": "8 kB",
    }
    fk_row = {
        "child_table": "child",
        "fk_name": "fk_child",
        "parent_table": "parent",
        "fk_definition": "FOREIGN KEY (parent_id) REFERENCES parent(id)",
    }
    dead_row = {
        "table_name": "dead_tbl",
        "dead_tuples": 12000,
        "live_tuples": 60000,
        "dead_tuple_pct": 16.7,
        "last_autovacuum": None,
        "last_vacuum": None,
    }
    hot_row = {
        "table_name": "hot_tbl",
        "total_updates": 200,
        "hot_updates": 10,
        "hot_ratio_pct": 5.0,
        "fillfactor": 100,
        "table_rows": 15000,
        "table_size": "10 MB",
    }
    redundant_row = {
        "table_name": "tbl",
        "redundant_index": "idx_a",
        "redundant_size": "32 kB",
        "covering_index": "idx_b",
        "redundant_def": "CREATE INDEX idx_a ON tbl (col1)",
        "covering_def": "CREATE INDEX idx_b ON tbl (col1, col2)",
    }
    low_usage_row = {
        "table_name": "big_tbl",
        "index_name": "idx_unused",
        "size": "64 kB",
        "index_scans": 1,
        "table_writes": 50000,
        "read_write_ratio": 0.00002,
        "table_rows": 100000,
        "table_size": "500 MB",
    }

    mock_conn = AsyncMock()
    # fetch is called once per check in check order:
    # invalid, unindexed_fks, dead_tuples, hot, redundant, low_usage
    mock_conn.fetch.side_effect = [
        [invalid_row],
        [fk_row],
        [dead_row],
        [hot_row],
        [redundant_row],
        [low_usage_row],
    ]

    with patch("mcp_pg_auditor.asyncpg.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn
        mock_conn.close = AsyncMock()
        auditor = AsyncPostgresHealthAuditor(dsn="postgresql://u:p@h/db")
        report = await auditor.run_full_audit(
            schemas=["public"],
            checks=[c.value for c in CheckName],
        )

    assert len(report.invalid_indexes) == 1
    assert len(report.unindexed_fks) == 1
    assert len(report.autovacuum_dead_tuples) == 1
    assert len(report.hot_fillfactor_issues) == 1
    assert len(report.redundant_indexes) == 1
    assert len(report.low_usage_indexes) == 1
    assert report.has_critical_issues is True


# ---------------------------------------------------------------------------
# 3.7 — run_full_audit with partial check selection
# ---------------------------------------------------------------------------


async def test_run_full_audit_partial_checks_skips_omitted():
    """Only invalid_indexes in checks → conn.fetch called once; all other lists empty."""
    invalid_row = {
        "child_table": "tbl",
        "invalid_index": "idx_bad",
        "index_size": "8 kB",
    }
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [invalid_row]

    with patch("mcp_pg_auditor.asyncpg.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn
        mock_conn.close = AsyncMock()
        auditor = AsyncPostgresHealthAuditor(dsn="postgresql://u:p@h/db")
        report = await auditor.run_full_audit(
            schemas=["public"],
            checks=[CheckName.INVALID_INDEXES.value],
        )

    assert mock_conn.fetch.call_count == 1
    assert len(report.invalid_indexes) == 1
    assert report.unindexed_fks == []
    assert report.autovacuum_dead_tuples == []
    assert report.hot_fillfactor_issues == []
    assert report.redundant_indexes == []
    assert report.low_usage_indexes == []


# ---------------------------------------------------------------------------
# 3.8 — has_critical_issues on PostgresHealthReport
# ---------------------------------------------------------------------------


def test_mcp_report_has_critical_issues_true():
    """Report with non-empty invalid_indexes → has_critical_issues is True."""
    issue = InvalidIndexIssue(
        child_table="tbl", invalid_index="idx_bad", index_size="8 kB"
    )
    report = PostgresHealthReport(
        database_alias="test",
        schemas_audited=["public"],
        has_critical_issues=True,
        invalid_indexes=[issue],
    )
    assert report.has_critical_issues is True


def test_mcp_report_has_critical_issues_false_warnings_only():
    """Report with non-empty hot_fillfactor_issues only → has_critical_issues False."""
    from mcp_pg_auditor import HotUpdateIssue

    hot = HotUpdateIssue(
        table_name="tbl",
        total_updates=100,
        hot_updates=5,
        hot_ratio_pct=5.0,
        fillfactor=100,
        fillfactor_warning=True,
        table_rows=50000,
        table_size="10 MB",
    )
    report = PostgresHealthReport(
        database_alias="test",
        schemas_audited=["public"],
        has_critical_issues=False,
        hot_fillfactor_issues=[hot],
    )
    assert report.has_critical_issues is False


# ---------------------------------------------------------------------------
# 3.9 — _print_cli_summary output
# ---------------------------------------------------------------------------


def test_print_cli_summary_shows_all_sections(capsys):
    """_print_cli_summary with one entry in each check shows all section headers."""
    from mcp_pg_auditor import HotUpdateIssue, LowUsageIndexIssue, RedundantIndexIssue

    report = PostgresHealthReport(
        database_alias="mydb",
        schemas_audited=["public"],
        has_critical_issues=True,
        invalid_indexes=[
            InvalidIndexIssue(child_table="t1", invalid_index="idx_bad", index_size="8 kB")
        ],
        unindexed_fks=[
            UnindexedFKIssue(
                child_table="child",
                fk_name="fk_x",
                parent_table="parent",
                fk_definition="FOREIGN KEY (pid) REFERENCES parent(id)",
            )
        ],
        autovacuum_dead_tuples=[
            DeadTuplesIssue(
                table_name="events",
                dead_tuples=15000,
                live_tuples=80000,
                dead_tuple_pct=15.8,
                last_autovacuum=None,
                last_vacuum=None,
            )
        ],
        hot_fillfactor_issues=[
            HotUpdateIssue(
                table_name="orders",
                total_updates=500,
                hot_updates=25,
                hot_ratio_pct=5.0,
                fillfactor=100,
                fillfactor_warning=True,
                table_rows=100000,
                table_size="50 MB",
            )
        ],
        redundant_indexes=[
            RedundantIndexIssue(
                table_name="users",
                redundant_index="idx_a",
                redundant_size="32 kB",
                covering_index="idx_b",
                redundant_def="CREATE INDEX idx_a ON users (col1)",
                covering_def="CREATE INDEX idx_b ON users (col1, col2)",
            )
        ],
        low_usage_indexes=[
            LowUsageIndexIssue(
                table_name="products",
                index_name="idx_unused",
                size="64 kB",
                index_scans=2,
                table_writes=30000,
                read_write_ratio=0.0001,
                table_rows=50000,
                table_size="200 MB",
            )
        ],
    )
    _print_cli_summary(report)
    out = capsys.readouterr().out

    # All 6 section headers must be present
    assert "Invalid Indexes" in out
    assert "Unindexed Foreign Keys" in out
    assert "Autovacuum" in out or "Dead Tuples" in out
    assert "HOT Updates" in out or "Fillfactor" in out
    assert "Redundant" in out
    assert "Low Usage" in out or "Unprofitable" in out


def test_print_cli_summary_shows_critical_flag(capsys):
    """has_critical_issues=True → 'YES' appears in the summary output."""
    report = PostgresHealthReport(
        database_alias="testdb",
        schemas_audited=["public"],
        has_critical_issues=True,
        invalid_indexes=[
            InvalidIndexIssue(child_table="t", invalid_index="idx_bad", index_size="8 kB")
        ],
    )
    _print_cli_summary(report)
    out = capsys.readouterr().out
    assert "YES" in out


# ---------------------------------------------------------------------------
# 3.10 — Schema list forwarded to ALL conn.fetch calls
# ---------------------------------------------------------------------------


async def test_run_full_audit_forwards_schema_list():
    """run_full_audit(schemas=["analytics"]) → all conn.fetch calls receive the schema list."""
    target_schema = ["analytics"]
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []

    with patch("mcp_pg_auditor.asyncpg.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn
        mock_conn.close = AsyncMock()
        auditor = AsyncPostgresHealthAuditor(dsn="postgresql://u:p@h/db")
        await auditor.run_full_audit(
            schemas=target_schema,
            checks=[c.value for c in CheckName],
        )

    assert mock_conn.fetch.call_count == len(list(CheckName))

    for call_args in mock_conn.fetch.call_args_list:
        positional = call_args[0]  # (query, *params)
        all_args = list(positional)
        assert target_schema in all_args, (
            f"Schema list {target_schema!r} not found in fetch call args: {all_args!r}"
        )
