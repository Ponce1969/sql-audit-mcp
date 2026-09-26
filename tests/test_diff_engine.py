"""Tests for the Differential Audit Engine (AuditDiff, compare_audit_reports)."""

from datetime import datetime, timezone

from sql_audit.application import compare_audit_reports, render_diff_text
from sql_audit.domain import (
    AuditReport,
    Evidence,
    ExecutionMetadata,
    Finding,
    Severity,
    compute_evidence_id,
)


def _make_finding(
    finding_id: str,
    check: str,
    object_name: str,
    values: dict,
    severity: Severity = Severity.MEDIUM,
) -> Finding:
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    eid = compute_evidence_id("pg_stat", check, values)
    ev = Evidence(
        source="pg_stat",
        observed_at=now,
        server_version="16.4",
        query_name=check,
        values=values,
        evidence_id=eid,
    )
    return Finding(
        finding_id=finding_id,
        check=check,
        severity=severity,
        object_type="table",
        object_name=object_name,
        reason="Test diagnosis",
        evidence=[ev],
    )


def test_diff_engine_detects_all_lifecycle_transitions():
    """Validates NEW, RESOLVED, PERSISTING_UNCHANGED, and PERSISTING_CHANGED transitions."""
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)

    # Finding 1: Will remain unchanged in both
    f_unchanged_prev = _make_finding(
        "PG-INDEX-INVALID:orders:idx_orders_broken",
        "invalid_indexes",
        "orders.idx_orders_broken",
        {"index_size": "10 MB"},
        Severity.CRITICAL,
    )
    f_unchanged_curr = _make_finding(
        "PG-INDEX-INVALID:orders:idx_orders_broken",
        "invalid_indexes",
        "orders.idx_orders_broken",
        {"index_size": "10 MB"},
        Severity.CRITICAL,
    )

    # Finding 2: In previous, but resolved in current
    f_resolved_prev = _make_finding(
        "PG-FK-UNINDEXED:order_items:fk_items_prod",
        "unindexed_fks",
        "order_items.fk_items_prod",
        {"fk_definition": "FOREIGN KEY (prod_id) REFERENCES products(id)"},
        Severity.HIGH,
    )

    # Finding 3: Persisting but changed evidence (metrics grew)
    f_changed_prev = _make_finding(
        "PG-VACUUM-DEAD-TUPLES:events",
        "autovacuum_dead_tuples",
        "events",
        {"dead_tuples": 15000, "live_tuples": 50000},
        Severity.HIGH,
    )
    f_changed_curr = _make_finding(
        "PG-VACUUM-DEAD-TUPLES:events",
        "autovacuum_dead_tuples",
        "events",
        {"dead_tuples": 35000, "live_tuples": 50000},
        Severity.HIGH,
    )

    # Finding 4: Brand new finding in current
    f_new_curr = _make_finding(
        "PG-HOT-FILLFACTOR:users",
        "hot_fillfactor",
        "users",
        {"hot_ratio_pct": 10.0, "fillfactor": 100},
        Severity.MEDIUM,
    )

    prev_report = AuditReport(
        audit_id="audit_run_001",
        database="prod_orders",
        server_version="16.4",
        observed_at=now,
        execution_metadata=ExecutionMetadata(),
        checks_executed=["invalid_indexes", "unindexed_fks", "autovacuum_dead_tuples"],
        has_critical_issues=True,
        findings=[f_unchanged_prev, f_resolved_prev, f_changed_prev],
    )

    curr_report = AuditReport(
        audit_id="audit_run_002",
        database="prod_orders",
        server_version="16.4",
        observed_at=now,
        execution_metadata=ExecutionMetadata(),
        checks_executed=["invalid_indexes", "autovacuum_dead_tuples", "hot_fillfactor"],
        has_critical_issues=True,
        findings=[f_unchanged_curr, f_changed_curr, f_new_curr],
    )

    diff = compare_audit_reports(prev_report, curr_report, compared_at=now)

    # Check summary counts
    assert diff.summary.new_count == 1
    assert diff.summary.resolved_count == 1
    assert diff.summary.unchanged_count == 1
    assert diff.summary.changed_count == 1
    assert diff.summary.total_previous == 3
    assert diff.summary.total_current == 3

    # Check lists
    assert len(diff.new_findings) == 1
    assert diff.new_findings[0].finding_id == "PG-HOT-FILLFACTOR:users"

    assert len(diff.resolved_findings) == 1
    assert diff.resolved_findings[0].finding_id == "PG-FK-UNINDEXED:order_items:fk_items_prod"

    assert len(diff.unchanged_findings) == 1
    assert diff.unchanged_findings[0].finding_id == "PG-INDEX-INVALID:orders:idx_orders_broken"

    assert len(diff.changed_findings) == 1
    cf = diff.changed_findings[0]
    assert cf.finding_id == "PG-VACUUM-DEAD-TUPLES:events"
    assert cf.previous_evidence_id != cf.current_evidence_id


def test_render_diff_text_formatting():
    """render_diff_text produces readable formatted output with all lifecycle categories."""
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    f_new = _make_finding(
        "PG-HOT-FILLFACTOR:users", "hot_fillfactor", "users", {"hot_ratio_pct": 10.0}
    )
    f_res = _make_finding(
        "PG-FK-UNINDEXED:items:fk", "unindexed_fks", "items.fk", {"def": "fk def"}
    )

    prev_report = AuditReport(
        audit_id="audit_1",
        database="db1",
        observed_at=now,
        checks_executed=[],
        has_critical_issues=False,
        findings=[f_res],
    )
    curr_report = AuditReport(
        audit_id="audit_2",
        database="db1",
        observed_at=now,
        checks_executed=[],
        has_critical_issues=False,
        findings=[f_new],
    )

    diff = compare_audit_reports(prev_report, curr_report, compared_at=now)
    text = render_diff_text(diff)

    assert "AUDIT DIFF REPORT: db1" in text
    assert "+1 new, -1 resolved" in text
    assert "[+] NEW FINDINGS:" in text
    assert "[-] RESOLVED FINDINGS:" in text


def test_cli_diff_flow(tmp_path, capsys):
    """CLI --diff correctly compares against a saved AuditReport JSON file."""
    import json
    from unittest.mock import MagicMock

    import audit_pg
    from audit_pg import DatabaseHealthReport, InvalidIndexIssue

    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    prev_report = AuditReport(
        audit_id="audit_prev_101",
        database="localhost",
        observed_at=now,
        checks_executed=["invalid"],
        has_critical_issues=False,
        findings=[],
    )
    prev_file = tmp_path / "prev_audit.json"
    prev_file.write_text(prev_report.model_dump_json(), encoding="utf-8")

    # Current audit finds a new invalid index (CRITICAL)
    curr_report = DatabaseHealthReport(
        invalid_indexes=[InvalidIndexIssue("orders", "idx_orders_bad", "50 MB")]
    )

    def factory(db_url, connect_timeout=10):
        mock = MagicMock()
        mock.run_audit.return_value = curr_report
        return mock

    exit_code = audit_pg.main(
        [
            "--url",
            "postgresql://u:p@localhost:5432/orders_db",
            "--diff",
            str(prev_file),
            "--canonical-json",
        ],
        auditor_factory=factory,
    )

    assert exit_code == 3  # Critical new finding
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["previous_audit_id"] == "audit_prev_101"
    assert data["summary"]["new_count"] == 1
    assert data["new_findings"][0]["finding_id"] == "PG-INDEX-INVALID:orders:idx_orders_bad"

