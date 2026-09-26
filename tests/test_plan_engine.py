"""Tests for PostgreSQL query plan analysis and impact simulation (pg_explain)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import audit_pg
from mcp_pg_auditor import pg_explain
from sql_audit.application import (
    analyze_execution_plan,
    render_plan_report_text,
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
