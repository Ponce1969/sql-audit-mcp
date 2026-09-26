"""
Phase 2 tests — TDD RED→GREEN for the 3 new CLI audit methods,
has_critical_issues property, and exit code 3.

Tasks covered:
  2.1  RED  _audit_invalid_indexes returns list[InvalidIndexIssue]
  2.2  GREEN implemented in audit_pg.py
  2.3  RED  _audit_unindexed_fks returns list[UnindexedFKIssue]
  2.4  GREEN implemented in audit_pg.py
  2.5  RED  _audit_autovacuum_dead_tuples handles datetime | None
  2.6  GREEN implemented in audit_pg.py
  2.7  RED  has_critical_issues truth-table
  2.8  GREEN confirmed by property already defined
  2.9  RED  main() returns 3 when has_critical_issues
  2.10 GREEN update main()
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import audit_pg
from audit_pg import (
    DatabaseHealthReport,
    DeadTuplesIssue,
    HotUpdateIssue,
    InvalidIndexIssue,
    UnindexedFKIssue,
)

# ---------------------------------------------------------------------------
# Helpers — minimal mock cursor that returns a fixed row list
# ---------------------------------------------------------------------------


def _make_cursor(rows: list[dict]) -> MagicMock:
    """Returns a MagicMock cursor whose fetchall() yields the given dicts."""
    cursor = MagicMock()
    cursor.fetchall.return_value = [dict(r) for r in rows]
    cursor.__enter__ = lambda s: s
    cursor.__exit__ = MagicMock(return_value=False)
    return cursor


# ---------------------------------------------------------------------------
# 2.1 / 2.2 — _audit_invalid_indexes
# ---------------------------------------------------------------------------


def test_audit_invalid_indexes_returns_typed_list():
    """2.1: _audit_invalid_indexes must return a list of InvalidIndexIssue."""
    row = {
        "child_table": "orders",
        "invalid_index": "idx_orders_stale",
        "index_size": "16 kB",
    }
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    cur = _make_cursor([row])

    result = auditor._audit_invalid_indexes(cur, schemas=["public"])

    assert len(result) == 1
    assert isinstance(result[0], InvalidIndexIssue)
    assert result[0].child_table == "orders"
    assert result[0].invalid_index == "idx_orders_stale"
    assert result[0].index_size == "16 kB"


def test_audit_invalid_indexes_empty_when_no_rows():
    """2.1: Empty cursor → empty list (no crash)."""
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    cur = _make_cursor([])
    result = auditor._audit_invalid_indexes(cur, schemas=["public"])
    assert result == []


def test_audit_invalid_indexes_schema_filter_passed_to_execute():
    """2.1: Schema list must be forwarded to cursor.execute()."""
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    cur = _make_cursor([])
    auditor._audit_invalid_indexes(cur, schemas=["myschema"])
    call_args = cur.execute.call_args
    # The schemas list should appear somewhere in the params
    params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", [])
    assert ["myschema"] in list(params) or ["myschema"] == list(params)


def test_audit_invalid_indexes_no_schema_filter_when_none():
    """2.1: When schemas=None, method should not crash and returns list."""
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    cur = _make_cursor([])
    result = auditor._audit_invalid_indexes(cur, schemas=None)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# 2.3 / 2.4 — _audit_unindexed_fks
# ---------------------------------------------------------------------------


def test_audit_unindexed_fks_returns_typed_list():
    """2.3: _audit_unindexed_fks must return a list of UnindexedFKIssue."""
    row = {
        "child_table": "order_items",
        "fk_name": "fk_order_items_order_id",
        "parent_table": "orders",
        "fk_definition": "FOREIGN KEY (order_id) REFERENCES orders(id)",
    }
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    cur = _make_cursor([row])

    result = auditor._audit_unindexed_fks(cur, schemas=["public"])

    assert len(result) == 1
    assert isinstance(result[0], UnindexedFKIssue)
    assert result[0].child_table == "order_items"
    assert result[0].fk_name == "fk_order_items_order_id"
    assert result[0].parent_table == "orders"
    assert "FOREIGN KEY" in result[0].fk_definition


def test_audit_unindexed_fks_empty_when_no_rows():
    """2.3: No unindexed FKs → empty list."""
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    result = auditor._audit_unindexed_fks(_make_cursor([]), schemas=["public"])
    assert result == []


def test_audit_unindexed_fks_no_schema_filter_when_none():
    """2.3: schemas=None must not crash."""
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    result = auditor._audit_unindexed_fks(_make_cursor([]), schemas=None)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# 2.5 / 2.6 — _audit_autovacuum_dead_tuples
# ---------------------------------------------------------------------------


def test_audit_dead_tuples_returns_typed_list():
    """2.5: _audit_autovacuum_dead_tuples must return a list of DeadTuplesIssue."""
    ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    row = {
        "table_name": "events",
        "dead_tuples": 50000,
        "live_tuples": 200000,
        "dead_tuple_pct": 20.0,
        "last_autovacuum": ts,
        "last_vacuum": None,  # covers datetime | None branch
    }
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    cur = _make_cursor([row])

    result = auditor._audit_autovacuum_dead_tuples(cur, schemas=["public"])

    assert len(result) == 1
    assert isinstance(result[0], DeadTuplesIssue)
    assert result[0].table_name == "events"
    assert result[0].dead_tuples == 50000
    assert result[0].live_tuples == 200000
    assert result[0].dead_tuple_pct == pytest.approx(20.0)
    assert result[0].last_autovacuum == ts
    assert result[0].last_vacuum is None  # None preserved


def test_audit_dead_tuples_all_nulls_ok():
    """2.5: Both last_autovacuum and last_vacuum can be None simultaneously."""
    row = {
        "table_name": "logs",
        "dead_tuples": 12000,
        "live_tuples": 80000,
        "dead_tuple_pct": 13.04,
        "last_autovacuum": None,
        "last_vacuum": None,
    }
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    result = auditor._audit_autovacuum_dead_tuples(_make_cursor([row]), schemas=["public"])
    assert result[0].last_autovacuum is None
    assert result[0].last_vacuum is None


def test_audit_dead_tuples_no_schema_filter_when_none():
    """2.5: schemas=None must not crash."""
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    result = auditor._audit_autovacuum_dead_tuples(_make_cursor([]), schemas=None)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# 2.7 / 2.8 — has_critical_issues truth table
# ---------------------------------------------------------------------------


def test_has_critical_issues_false_when_empty():
    """2.7: No critical issues when all fields are empty."""
    assert DatabaseHealthReport().has_critical_issues is False


def test_has_critical_issues_true_when_invalid_indexes():
    """2.7: has_critical_issues is True when invalid_indexes is non-empty."""
    issue = InvalidIndexIssue("orders", "idx_stale", "16 kB")
    report = DatabaseHealthReport(invalid_indexes=[issue])
    assert report.has_critical_issues is True


def test_has_critical_issues_true_when_unindexed_fks():
    """2.7: has_critical_issues is True when unindexed_fks is non-empty."""
    issue = UnindexedFKIssue("items", "fk_x", "orders", "FOREIGN KEY (x) REFERENCES orders(id)")
    report = DatabaseHealthReport(unindexed_fks=[issue])
    assert report.has_critical_issues is True


def test_has_critical_issues_true_when_dead_tuples():
    """2.7: has_critical_issues is True when autovacuum_dead_tuples is non-empty."""
    issue = DeadTuplesIssue("events", 50000, 200000, 20.0)
    report = DatabaseHealthReport(autovacuum_dead_tuples=[issue])
    assert report.has_critical_issues is True


def test_has_critical_issues_false_when_only_warnings():
    """2.7: hot/redundant/low-usage issues alone do NOT trigger has_critical_issues."""
    hot = HotUpdateIssue("orders", 100, 10, 10.0, 100, True, 50000, "12 MB")
    report = DatabaseHealthReport(hot_issues=[hot])
    assert report.has_critical_issues is False


# ---------------------------------------------------------------------------
# 2.9 / 2.10 — exit code 3
# ---------------------------------------------------------------------------


def _make_factory_with_report(report: DatabaseHealthReport):
    def factory(db_url, connect_timeout=10):
        class FakeAuditor:
            def run_audit(self, **kwargs):
                return report

        return FakeAuditor()

    return factory


def test_main_returns_3_when_critical_issues():
    """2.9: main() must return exit code 3 when has_critical_issues is True."""
    critical_report = DatabaseHealthReport(
        invalid_indexes=[InvalidIndexIssue("orders", "idx_stale", "16 kB")]
    )
    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/db"],
        auditor_factory=_make_factory_with_report(critical_report),
    )
    assert code == 3


def test_main_returns_3_takes_precedence_over_2():
    """2.9: exit 3 takes precedence even when hot issues are also present."""
    mixed_report = DatabaseHealthReport(
        invalid_indexes=[InvalidIndexIssue("orders", "idx_stale", "16 kB")],
        hot_issues=[HotUpdateIssue("orders", 100, 10, 10.0, 100, True, 50000, "12 MB")],
    )
    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/db"],
        auditor_factory=_make_factory_with_report(mixed_report),
    )
    assert code == 3


def test_main_returns_2_when_warnings_only():
    """2.9: exit 2 preserved when only warning-level issues (no criticals)."""
    report = DatabaseHealthReport(
        hot_issues=[HotUpdateIssue("orders", 100, 10, 10.0, 100, True, 50000, "12 MB")]
    )
    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/db"],
        auditor_factory=_make_factory_with_report(report),
    )
    assert code == 2


def test_main_returns_0_when_clean():
    """2.9: exit 0 preserved when no issues of any kind."""
    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/db"],
        auditor_factory=_make_factory_with_report(DatabaseHealthReport()),
    )
    assert code == 0
