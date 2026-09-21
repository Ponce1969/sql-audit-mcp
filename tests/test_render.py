import json
from decimal import Decimal

import audit_pg
from audit_pg import (
    DatabaseHealthReport,
    HotUpdateIssue,
    LowUsageIndexIssue,
)


def test_render_text_empty_report():
    report = DatabaseHealthReport()
    text = audit_pg.render_text(report)
    assert "REDUNDANT / PREFIX INDEXES FOUND: 0" in text
    assert "No redundant indexes detected" in text
    assert "LOW HOT UPDATE EFFICIENCY: 0" in text
    assert "No tables with low HOT update ratio" in text
    assert "UNPROFITABLE / HIGH-WRITE LOW-READ INDEXES: 0" in text
    assert "No unprofitable indexes detected" in text


def test_render_text_with_issues():
    report = DatabaseHealthReport(
        hot_issues=[HotUpdateIssue("orders", 100, 20, 20.0, 100, True, 50000, "12 MB")]
    )
    text = audit_pg.render_text(report)
    assert "* Table: orders" in text
    assert "WARNING: Default 100% fillfactor prevents HOT" in text
    assert "ALTER TABLE orders SET (fillfactor = 85)" in text
    assert "Rows: 50000" in text
    assert "Table size: 12 MB" in text


def test_render_text_low_usage_context():
    report = DatabaseHealthReport(
        low_usage_indexes=[
            LowUsageIndexIssue("events", "idx_events_x", "32 kB", 3, 5000, 0.0006, 50000, "12 MB")
        ]
    )
    text = audit_pg.render_text(report)
    assert "* Table: events (Rows: 50000 | Size: 12 MB)" in text


def test_render_json_schema_stability():
    report = DatabaseHealthReport(
        hot_issues=[HotUpdateIssue("orders", 100, 20, 20.0, 100, True, 50000, "12 MB")],
        low_usage_indexes=[
            LowUsageIndexIssue("events", "idx_events_x", "32 kB", 3, 5000, 0.0006, 50000, "12 MB")
        ],
    )
    data = audit_pg.render_json(report, database="dbhost", checks=["redundant", "hot", "low-usage"])
    assert data == {
        "database": "dbhost",
        "checks": ["redundant", "hot", "low-usage"],
        "summary": {"redundant_indexes": 0, "hot_issues": 1, "low_usage_indexes": 1},
        "issues": {
            "redundant": [],
            "hot": [
                {
                    "table_name": "orders",
                    "total_updates": 100,
                    "hot_updates": 20,
                    "hot_ratio_pct": 20.0,
                    "fillfactor": 100,
                    "fillfactor_warning": True,
                    "table_rows": 50000,
                    "table_size": "12 MB",
                }
            ],
            "low-usage": [
                {
                    "table_name": "events",
                    "index_name": "idx_events_x",
                    "size": "32 kB",
                    "index_scans": 3,
                    "table_writes": 5000,
                    "read_write_ratio": 0.0006,
                    "table_rows": 50000,
                    "table_size": "12 MB",
                }
            ],
        },
    }


def test_render_json_empty_report():
    report = DatabaseHealthReport()
    data = audit_pg.render_json(report, database="dbhost", checks=["redundant", "hot", "low-usage"])
    assert data["summary"] == {"redundant_indexes": 0, "hot_issues": 0, "low_usage_indexes": 0}
    assert data["issues"] == {"redundant": [], "hot": [], "low-usage": []}


def test_render_json_converts_decimal_to_float():
    report = DatabaseHealthReport(
        hot_issues=[HotUpdateIssue("orders", 100, 50, Decimal("50.00"), 100, False, 50000, "12 MB")]
    )
    data = audit_pg.render_json(report, database="dbhost", checks=["hot"])
    value = data["issues"]["hot"][0]["hot_ratio_pct"]
    assert isinstance(value, float)
    assert value == 50.0
    json.dumps(data)  # must be serializable


def test_has_issues():
    assert audit_pg.has_issues(DatabaseHealthReport()) is False
    issue = HotUpdateIssue("t", 1, 1, 100.0, 100, False, 50000, "12 MB")
    assert audit_pg.has_issues(DatabaseHealthReport(hot_issues=[issue])) is True
