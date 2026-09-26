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
        "has_critical_issues": False,
        "summary": {
            "invalid_indexes": 0,
            "unindexed_fks": 0,
            "autovacuum_dead_tuples": 0,
            "redundant_indexes": 0,
            "hot_issues": 1,
            "low_usage_indexes": 1,
        },
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
    assert data["summary"] == {
        "invalid_indexes": 0,
        "unindexed_fks": 0,
        "autovacuum_dead_tuples": 0,
        "redundant_indexes": 0,
        "hot_issues": 0,
        "low_usage_indexes": 0,
    }
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
    # Critical-only issue
    from audit_pg import InvalidIndexIssue

    inv = InvalidIndexIssue("t", "idx_bad", "10 MB")
    assert audit_pg.has_issues(DatabaseHealthReport(invalid_indexes=[inv])) is True


def test_render_text_with_critical_issues():
    from audit_pg import DeadTuplesIssue, InvalidIndexIssue, UnindexedFKIssue

    report = DatabaseHealthReport(
        invalid_indexes=[InvalidIndexIssue("orders", "idx_orders_bad", "10 MB")],
        unindexed_fks=[
            UnindexedFKIssue(
                "order_items", "fk_order", "orders", "FOREIGN KEY (o_id) REFERENCES orders(id)"
            )
        ],
        autovacuum_dead_tuples=[DeadTuplesIssue("logs", 20000, 100000, 16.67, None, None)],
    )
    text = audit_pg.render_text(report)
    assert "INVALID INDEXES (indisvalid = false): 1" in text
    assert "* Table: orders" in text
    assert "Invalid index: idx_orders_bad" in text
    assert "UNINDEXED FOREIGN KEYS: 1" in text
    assert "* Table: order_items -> orders" in text
    assert "AUTOVACUUM & DEAD TUPLES LAG: 1" in text
    assert "Dead Tuples: 20,000" in text


def test_render_quiet_with_all_checks():
    from audit_pg import InvalidIndexIssue, UnindexedFKIssue

    report = DatabaseHealthReport(
        invalid_indexes=[InvalidIndexIssue("orders", "idx_bad", "10 MB")],
        unindexed_fks=[UnindexedFKIssue("items", "fk_items", "orders", "def")],
    )
    quiet = audit_pg.render_quiet(report)
    assert "invalid_indexes: 1" in quiet
    assert "unindexed_fks: 1" in quiet
    assert "autovacuum_dead_tuples: 0" in quiet
    assert "redundant_indexes: 0" in quiet
    assert "hot_issues: 0" in quiet
    assert "low_usage_indexes: 0" in quiet


# ---------------------------------------------------------------------------
# Phase 1: hot_ratio_pct field-type contract (task 1.7)
# After QC-04 fix the DB emits ::float directly, so the field must accept
# a plain Python float without going through _jsonable().
# ---------------------------------------------------------------------------


def test_hot_ratio_pct_accepts_native_float():
    """1.7: HotUpdateIssue.hot_ratio_pct must be stored and returned as float."""
    issue = HotUpdateIssue(
        table_name="orders",
        total_updates=200,
        hot_updates=100,
        hot_ratio_pct=50.0,  # native float — no Decimal wrapper
        fillfactor=100,
        fillfactor_warning=True,
        table_rows=50000,
        table_size="12 MB",
    )
    assert isinstance(issue.hot_ratio_pct, float)
    assert issue.hot_ratio_pct == 50.0
    # Confirm it round-trips through render_json without _jsonable() assistance
    report = audit_pg.DatabaseHealthReport(hot_issues=[issue])
    data = audit_pg.render_json(report, database="h", checks=["hot"])
    assert data["issues"]["hot"][0]["hot_ratio_pct"] == 50.0
    assert isinstance(data["issues"]["hot"][0]["hot_ratio_pct"], float)
