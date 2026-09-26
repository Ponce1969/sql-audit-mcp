"""Tests for physical bloat estimation (pg_bloat)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import audit_pg
from mcp_pg_auditor import pg_bloat
from sql_audit.application import (
    bloat_report_to_findings,
    build_bloat_report,
    render_bloat_report_text,
)
from sql_audit.domain import Severity


def test_bloat_engine_empty_when_no_bloat():
    """Returns empty report with 0 bloated objects when no tables exceed threshold."""
    report = build_bloat_report([], [], database="prod_db")
    assert len(report.tables) == 0
    assert len(report.indexes) == 0
    assert report.total_table_bloat_bytes == 0
    assert report.total_index_bloat_bytes == 0

    text = render_bloat_report_text(report)
    assert "0 bloated tables or indexes detected (clean)" in text


def test_bloat_engine_detects_table_and_index_bloat():
    """Correctly categorizes bloat and flags objects meeting thresholds."""
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)

    table_rows = [
        {
            "schema_name": "public",
            "table_name": "orders",
            "table_size_bytes": 100_000_000,
            "expected_size_bytes": 40_000_000,
            "bloat_bytes": 60_000_000,
            "bloat_ratio_pct": 60.0,
        },
        {
            "schema_name": "public",
            "table_name": "small_tbl",
            "table_size_bytes": 1_000_000,
            "expected_size_bytes": 800_000,
            "bloat_bytes": 200_000,
            "bloat_ratio_pct": 20.0,
        },
    ]

    index_rows = [
        {
            "schema_name": "public",
            "table_name": "orders",
            "index_name": "idx_orders_customer",
            "index_size_bytes": 50_000_000,
            "expected_size_bytes": 20_000_000,
            "bloat_bytes": 30_000_000,
            "bloat_ratio_pct": 60.0,
        }
    ]

    report = build_bloat_report(
        table_rows=table_rows,
        index_rows=index_rows,
        database="prod_db",
        min_bloat_bytes=10_000_000,
        min_bloat_ratio_pct=20.0,
        observed_at=now,
    )

    assert report.total_table_bloat_bytes == 60_000_000
    assert report.total_index_bloat_bytes == 30_000_000

    # orders is bloated, small_tbl is below 10MB threshold
    assert report.tables[0].table_name == "orders"
    assert report.tables[0].is_bloated is True
    assert report.tables[1].table_name == "small_tbl"
    assert report.tables[1].is_bloated is False

    assert report.indexes[0].index_name == "idx_orders_customer"
    assert report.indexes[0].is_bloated is True

    # Validate findings generation
    findings = bloat_report_to_findings(report, server_version="16.4")
    assert len(findings) == 2

    tbl_f = next(f for f in findings if f.object_type == "table")
    assert tbl_f.finding_id == "PG-BLOAT-TABLE:public.orders"
    assert tbl_f.severity == Severity.HIGH
    assert "60.0% bloat" in tbl_f.reason
    assert tbl_f.evidence[0].evidence_id.startswith("sha256:")

    idx_f = next(f for f in findings if f.object_type == "index")
    assert idx_f.finding_id == "PG-BLOAT-INDEX:public.idx_orders_customer"
    assert idx_f.severity == Severity.MEDIUM
    assert idx_f.evidence[0].evidence_id.startswith("sha256:")

    # Validate text rendering
    rendered = render_bloat_report_text(report)
    assert "POSTGRESQL PHYSICAL BLOAT ESTIMATION: prod_db" in rendered
    assert "- public.orders: 57.2 MB bloat (60.0%)" in rendered
    assert "- public.idx_orders_customer (on orders): 28.6 MB bloat" in rendered


def test_cli_bloat_clean_flow(capsys):
    """CLI --bloat returns exit code 0 when no bloat exceeds thresholds."""
    report = build_bloat_report([], [], database="cleandb")

    def factory(db_url, connect_timeout=10):
        mock = MagicMock()
        mock.audit_bloat.return_value = report
        return mock

    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/cleandb", "--bloat"],
        auditor_factory=factory,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "0 bloated tables or indexes detected (clean)" in out


def test_cli_bloat_detected_flow(capsys):
    """CLI --bloat returns exit code 2 when bloated tables or indexes exist."""
    import json

    table_rows = [
        {
            "schema_name": "public",
            "table_name": "events",
            "table_size_bytes": 80_000_000,
            "expected_size_bytes": 30_000_000,
            "bloat_bytes": 50_000_000,
            "bloat_ratio_pct": 62.5,
        }
    ]
    report = build_bloat_report(table_rows, [], database="testdb")

    def factory(db_url, connect_timeout=10):
        mock = MagicMock()
        mock.audit_bloat.return_value = report
        return mock

    # 1. Text mode
    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/testdb", "--bloat"],
        auditor_factory=factory,
    )
    assert code == 2
    out = capsys.readouterr().out
    assert "BLOATED TABLES (1)" in out
    assert "events" in out

    # 2. JSON mode
    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/testdb", "--bloat", "--json"],
        auditor_factory=factory,
    )
    assert code == 2
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["total_table_bloat_bytes"] == 50_000_000
    assert len(data["tables"]) == 1
    assert data["tables"][0]["is_bloated"] is True


async def test_mcp_pg_bloat_tool(monkeypatch):
    """FastMCP pg_bloat tool returns BloatReport correctly."""
    report = build_bloat_report([], [], database="mcp_test")

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/mcp_test")
    monkeypatch.setattr(
        "mcp_pg_auditor.AsyncPostgresHealthAuditor.audit_bloat",
        AsyncMock(return_value=report),
    )

    result = await pg_bloat(db_alias="default")
    assert result.total_table_bloat_bytes == 0
    assert result.total_index_bloat_bytes == 0
    assert len(result.tables) == 0
    assert len(result.indexes) == 0
