"""Unit tests for sql_audit.domain models and hashing stability."""

from datetime import datetime, timezone

from sql_audit.domain import (
    AuditReport,
    Evidence,
    ExecutionMetadata,
    Finding,
    Severity,
    compute_evidence_id,
    compute_stable_finding_id,
)


def test_finding_id_stable_across_counter_variations():
    """finding_id must remain identical even when dead_tuples or scans change."""
    fid_run1 = compute_stable_finding_id("PG-VACUUM-DEAD-TUPLES", "table", "public.events")
    fid_run2 = compute_stable_finding_id("PG-VACUUM-DEAD-TUPLES", "table", "public.events")
    assert fid_run1 == "PG-VACUUM-DEAD-TUPLES:public.events"
    assert fid_run1 == fid_run2


def test_evidence_id_changes_when_metrics_change():
    """evidence_id must reflect exact runtime counters and change when they change."""
    eid_1 = compute_evidence_id(
        source="pg_stat",
        query_name="autovacuum_dead_tuples",
        values={"dead_tuples": 15000, "live_tuples": 50000, "dead_tuple_pct": 23.08},
    )
    eid_2 = compute_evidence_id(
        source="pg_stat",
        query_name="autovacuum_dead_tuples",
        values={"dead_tuples": 25000, "live_tuples": 50000, "dead_tuple_pct": 33.33},
    )
    eid_1_repeat = compute_evidence_id(
        source="pg_stat",
        query_name="autovacuum_dead_tuples",
        values={"dead_tuples": 15000, "live_tuples": 50000, "dead_tuple_pct": 23.08},
    )

    assert eid_1.startswith("sha256:")
    assert eid_2.startswith("sha256:")
    assert eid_1 != eid_2
    assert eid_1 == eid_1_repeat


def test_audit_report_contract_instantiation():
    """AuditReport serves as root contract with required metadata."""
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    ev = Evidence(
        source="pg_catalog",
        observed_at=now,
        server_version="16.4",
        query_name="invalid_indexes",
        values={"index_size": "24 MB"},
        evidence_id="sha256:abc123456",
    )
    finding = Finding(
        finding_id="PG-INDEX-INVALID:orders:idx_orders_broken",
        check="invalid_indexes",
        severity=Severity.CRITICAL,
        object_type="index",
        object_name="orders.idx_orders_broken",
        reason="Aborted concurrent index build left unvalidated index",
        evidence=[ev],
    )
    report = AuditReport(
        audit_id="01J8K3XYZ90",
        database="test_db",
        server_version="16.4",
        observed_at=now,
        execution_metadata=ExecutionMetadata(duration_ms=45.2, min_table_rows=10000),
        checks_executed=["invalid_indexes"],
        has_critical_issues=True,
        summary={"invalid_indexes": 1},
        findings=[finding],
        errors=[],
    )

    dumped = report.model_dump()
    assert dumped["schema_version"] == "1.0.0"
    assert dumped["audit_id"] == "01J8K3XYZ90"
    assert dumped["findings"][0]["severity"] == "critical"
    assert dumped["findings"][0]["finding_id"] == "PG-INDEX-INVALID:orders:idx_orders_broken"
    assert dumped["findings"][0]["evidence"][0]["evidence_id"] == "sha256:abc123456"
