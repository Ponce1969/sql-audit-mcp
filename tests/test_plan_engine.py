"""Tests for PostgreSQL query plan analysis and impact simulation (pg_explain)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import audit_pg
from mcp_pg_auditor import pg_explain
from sql_audit.application import (
    analyze_execution_plan,
    render_plan_report_text,
    validate_explain_query,
)


def test_plan_engine_clean_index_scan():
    """Execution plan with efficient index scan generates 0 warnings."""
    raw_plan = [
        {
            "Plan": {
                "Node Type": "Index Scan",
                "Relation Name": "users",
                "Index Name": "idx_users_email",
                "Total Cost": 8.45,
                "Plan Rows": 1,
                "Actual Rows": 1,
                "Shared Hit Blocks": 4,
                "Shared Read Blocks": 0,
            },
            "Planning Time": 0.15,
            "Execution Time": 0.08,
        }
    ]

    report = analyze_execution_plan(raw_plan, query="SELECT * FROM users WHERE email = 'a@b.com'")
    assert report.is_analyzed is True
    assert report.summary.total_cost == 8.45
    assert report.summary.execution_time_ms == 0.08
    assert report.summary.shared_hit_blocks == 4
    assert len(report.summary.warnings) == 0

    text = render_plan_report_text(report)
    assert "PLAN HEALTH: No critical anomalies or bottlenecks detected." in text


def test_plan_engine_detects_bottlenecks():
    """Correctly detects large seq scan, work_mem disk spill, and estimation skew."""
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)

    raw_plan = [
        {
            "Plan": {
                "Node Type": "Sort",
                "Sort Method": "external merge Disk: 15240kB",
                "Total Cost": 45000.0,
                "Plan Rows": 500,
                "Actual Rows": 85000,
                "Temp Written Blocks": 1905,
                "Shared Hit Blocks": 100,
                "Shared Read Blocks": 6000,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "events",
                        "Total Cost": 25000.0,
                        "Plan Rows": 500,
                        "Actual Rows": 85000,
                        "Shared Hit Blocks": 100,
                        "Shared Read Blocks": 6000,
                    }
                ],
            },
            "Planning Time": 0.45,
            "Execution Time": 1450.2,
        }
    ]

    report = analyze_execution_plan(
        raw_plan,
        query="SELECT * FROM events ORDER BY payload",
        database="prod_db",
        observed_at=now,
    )

    s = report.summary
    assert s.has_seq_scan_large_table is True
    assert s.has_disk_sort is True
    assert s.has_high_estimation_skew is True
    assert s.temp_written_blocks == 1905
    assert len(s.warnings) >= 3

    warning_types = [w.warning_type for w in s.warnings]
    assert "seq_scan_large_table" in warning_types
    assert "workmem_disk_spill" in warning_types
    assert "estimation_skew_underestimated" in warning_types

    # Validate formatted text output
    rendered = render_plan_report_text(report)
    assert "POSTGRESQL EXECUTION PLAN AUDIT: prod_db" in rendered
    assert "Execution Time: 1450.20 ms" in rendered
    assert "Temp Written:   1,905 blocks" in rendered
    assert "[HIGH] seq_scan_large_table" in rendered
    assert "[HIGH] workmem_disk_spill" in rendered


def test_cli_explain_clean_flow(capsys):
    """CLI --explain returns exit code 0 when plan has no warnings."""
    raw_plan = [
        {
            "Plan": {
                "Node Type": "Result",
                "Total Cost": 0.01,
                "Plan Rows": 1,
            }
        }
    ]
    report = analyze_execution_plan(raw_plan, query="SELECT 1")

    def factory(db_url, connect_timeout=10):
        mock = MagicMock()
        mock.explain_query.return_value = report
        return mock

    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/cleandb", "--explain", "SELECT 1"],
        auditor_factory=factory,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "PLAN HEALTH: No critical anomalies or bottlenecks detected." in out


def test_cli_explain_high_risk_flow(capsys):
    """CLI --explain returns exit code 3 when high-risk plan warnings are found."""
    import json

    raw_plan = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "huge_table",
                "Total Cost": 50000.0,
                "Plan Rows": 100000,
            }
        }
    ]
    report = analyze_execution_plan(raw_plan, query="SELECT * FROM huge_table")

    def factory(db_url, connect_timeout=10):
        mock = MagicMock()
        mock.explain_query.return_value = report
        return mock

    # 1. Text mode
    code = audit_pg.main(
        [
            "--url",
            "postgresql://u:p@localhost:5432/testdb",
            "--explain",
            "SELECT * FROM huge_table",
        ],
        auditor_factory=factory,
    )
    assert code == 3
    out = capsys.readouterr().out
    assert "[HIGH] seq_scan_large_table" in out

    # 2. JSON mode
    code = audit_pg.main(
        [
            "--url",
            "postgresql://u:p@localhost:5432/testdb",
            "--explain",
            "SELECT * FROM huge_table",
            "--json",
        ],
        auditor_factory=factory,
    )
    assert code == 3
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["summary"]["has_seq_scan_large_table"] is True
    assert len(data["summary"]["warnings"]) == 1


async def test_mcp_pg_explain_tool(monkeypatch):
    """FastMCP pg_explain tool executes and returns PlanAnalysisReport."""
    raw_plan = [
        {
            "Plan": {
                "Node Type": "Result",
                "Total Cost": 0.01,
            }
        }
    ]
    report = analyze_execution_plan(raw_plan, query="SELECT 1")

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/mcp_test")
    monkeypatch.setattr(
        "mcp_pg_auditor.AsyncPostgresHealthAuditor.explain_query",
        AsyncMock(return_value=report),
    )

    result = await pg_explain(query="SELECT 1", db_alias="default")
    assert result.query == "SELECT 1"
    assert result.summary.total_cost == 0.01
    assert len(result.summary.warnings) == 0


def test_cli_rejects_analyze_flag():
    """CLI argparse rejects --analyze flag under strictly passive contract."""
    import pytest

    parser = audit_pg.build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--explain", "SELECT 1", "--analyze"])
    assert exc_info.value.code == 2


def test_mcp_and_auditor_signatures_have_no_analyze_parameter():
    """Verify that neither pg_explain nor explain_query accept an analyze parameter."""
    import inspect

    import mcp_pg_auditor

    # MCP tool
    tool_sig = inspect.signature(pg_explain)
    assert "analyze" not in tool_sig.parameters, "pg_explain must not expose 'analyze' parameter"

    # Sync auditor
    sync_sig = inspect.signature(audit_pg.PostgresHealthAuditor.explain_query)
    assert "analyze" not in sync_sig.parameters, (
        "PostgresHealthAuditor.explain_query must not accept 'analyze' parameter"
    )

    # Async auditor
    async_sig = inspect.signature(mcp_pg_auditor.AsyncPostgresHealthAuditor.explain_query)
    assert "analyze" not in async_sig.parameters, (
        "AsyncPostgresHealthAuditor.explain_query must not accept 'analyze' parameter"
    )


def test_sync_explain_query_uses_strictly_passive_sql(monkeypatch):
    """Sync explain_query issues only static EXPLAIN without transaction or rollback."""
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = [
        [
            {
                "Plan": {
                    "Node Type": "Result",
                    "Total Cost": 0.01,
                    "Plan Rows": 1,
                }
            }
        ]
    ]

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    monkeypatch.setattr(audit_pg.psycopg2, "connect", MagicMock(return_value=mock_conn))
    monkeypatch.setattr(audit_pg, "HAS_PSYCOPG2", True)

    auditor = audit_pg.PostgresHealthAuditor(db_url="postgresql://u:p@localhost:5432/db")
    auditor.explain_query(query="SELECT 1")

    # Verify executed SQL commands
    executed_statements = [call.args[0] for call in mock_cursor.execute.call_args_list]
    for stmt in executed_statements:
        assert "ANALYZE" not in stmt, f"ANALYZE must not be executed: {stmt}"
        assert "BEGIN" not in stmt, f"Explicit BEGIN must not be executed: {stmt}"
        assert "ROLLBACK" not in stmt, f"ROLLBACK must not be executed: {stmt}"
        assert "BUFFERS" not in stmt, (
            f"BUFFERS without ANALYZE is invalid in PostgreSQL: {stmt}"
        )

    assert any(
        stmt == "EXPLAIN (COSTS, VERBOSE, FORMAT JSON) SELECT 1"
        for stmt in executed_statements
    )


async def test_async_explain_query_uses_strictly_passive_sql(monkeypatch):
    """Async explain_query issues only static EXPLAIN without transaction or rollback."""
    import mcp_pg_auditor

    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = (
        '[{"Plan": {"Node Type": "Result", "Total Cost": 0.01, "Plan Rows": 1}}]'
    )

    monkeypatch.setattr(mcp_pg_auditor.asyncpg, "connect", AsyncMock(return_value=mock_conn))

    auditor = mcp_pg_auditor.AsyncPostgresHealthAuditor(
        dsn="postgresql://u:p@localhost:5432/db"
    )
    await auditor.explain_query(query="SELECT 1", db_alias="default")

    # Verify transaction was never started
    mock_conn.transaction.assert_not_called()

    # Verify executed SQL commands
    assert mock_conn.fetchval.call_count == 1
    executed_stmt = mock_conn.fetchval.call_args.args[0]
    assert "ANALYZE" not in executed_stmt, f"ANALYZE must not be executed: {executed_stmt}"
    assert "BUFFERS" not in executed_stmt, f"BUFFERS must not be executed: {executed_stmt}"
    assert executed_stmt == "EXPLAIN (COSTS, VERBOSE, FORMAT JSON) SELECT 1"


def test_parser_preserves_compatibility_with_pre_analyzed_plans():
    """analyze_execution_plan parser preserves ability to process plans with execution metrics."""
    raw_plan = [
        {
            "Plan": {
                "Node Type": "Index Scan",
                "Relation Name": "users",
                "Total Cost": 8.45,
                "Plan Rows": 1,
                "Actual Rows": 1,
                "Actual Total Time": 0.05,
                "Shared Hit Blocks": 4,
            },
            "Execution Time": 0.08,
        }
    ]
    report = analyze_execution_plan(raw_plan, query="SELECT * FROM users")
    assert report.is_analyzed is True
    assert report.summary.execution_time_ms == 0.08
    assert report.summary.shared_hit_blocks == 4


def test_validate_explain_query_valid():
    """validate_explain_query accepts single-statement queries and handles semicolons properly."""
    # Simple query
    assert validate_explain_query("SELECT 1") == "SELECT 1"

    # Query with trailing semicolon
    assert validate_explain_query("SELECT * FROM users;") == "SELECT * FROM users"
    assert validate_explain_query("  SELECT * FROM users;  ") == "SELECT * FROM users"

    # Query with semicolons inside literal quotes
    query_with_quotes = "SELECT * FROM logs WHERE message = 'semicolon; here'"
    assert validate_explain_query(query_with_quotes) == query_with_quotes


def test_validate_explain_query_rejects_empty_and_multistatement():
    """validate_explain_query raises ValueError on empty or multi-statement queries."""
    with pytest.raises(ValueError, match="empty"):
        validate_explain_query("")

    with pytest.raises(ValueError, match="empty"):
        validate_explain_query("   ")

    with pytest.raises(ValueError, match="empty"):
        validate_explain_query(";;;")

    with pytest.raises(ValueError, match="Multi-statement"):
        validate_explain_query("SELECT 1; DROP TABLE users;")

    with pytest.raises(ValueError, match="Multi-statement"):
        validate_explain_query("SELECT 1; DELETE FROM logs")


def test_plan_engine_detects_low_cache_hit_and_nested_loop():
    """Detects low cache hit ratio when shared_read >= 2000 and hit_ratio < 80%."""
    raw_plan = [
        {
            "Plan": {
                "Node Type": "Nested Loop",
                "Total Cost": 15000.0,
                "Plan Rows": 100,
                "Actual Rows": 100,
                "Actual Loops": 15000,
                "Actual Total Time": 1200.0,
                "Shared Hit Blocks": 500,
                "Shared Read Blocks": 3000,
                "Plans": [],
            },
            "Execution Time": 1250.0,
        }
    ]
    report = analyze_execution_plan(raw_plan, query="SELECT * FROM a JOIN b ON a.id = b.a_id")
    warning_types = [w.warning_type for w in report.summary.warnings]
    assert "low_cache_hit_ratio" in warning_types
    assert "nested_loop_high_loops" in warning_types

