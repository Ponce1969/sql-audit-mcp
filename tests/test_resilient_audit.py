"""Tests for T-03: Resilient Partial Audit (RESILIENT_PARTIAL_AUDIT).

Verifies that isolated check failures preserve findings from successful checks,
record errors with check origin, mark the report unambiguously as partial/degraded,
prevent incomplete runs from being interpreted as healthy, allow global failures
to remain fatal, and distinguish partial audits from complete audits during diffing.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import audit_pg
import mcp_pg_auditor
from audit_pg import (
    DatabaseConnectionError,
    DatabaseHealthReport,
    HotUpdateIssue,
    InvalidIndexIssue,
    PostgresHealthAuditor,
    has_issues,
)
from mcp_pg_auditor import (
    AsyncPostgresHealthAuditor,
    CheckName,
)
from sql_audit.application.diff_service import compare_audit_reports
from sql_audit.application.mapping import (
    build_audit_report,
)
from sql_audit.domain import (
    compute_evidence_id,
    compute_stable_finding_id,
)
from sql_audit.domain.models import (
    AuditReport,
    Evidence,
    ExecutionMetadata,
    Finding,
    Severity,
)


def _make_dummy_finding(check: str, name: str) -> Finding:
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    ev = Evidence(
        source="pg_catalog",
        observed_at=now,
        server_version="16.4",
        query_name=check,
        values={"metric": 42},
        evidence_id=compute_evidence_id("pg_catalog", check, {"metric": 42}),
    )
    return Finding(
        finding_id=compute_stable_finding_id(f"PG-{check.upper()}", name),
        check=check,
        severity=Severity.HIGH,
        object_type="table",
        object_name=name,
        reason="Test finding",
        evidence=[ev],
    )


# --- 1. Completely Successful Audit ---


def test_complete_audit_is_not_marked_as_partial():
    """A fully successful audit preserves normal state and is not partial."""
    report = build_audit_report(
        database="test_db",
        checks_executed=["invalid_indexes", "unindexed_fks"],
        findings=[],
    )
    assert report.is_partial is False
    assert report.errors == []
    assert report.checks_failed == []
    assert report.checks_executed == ["invalid_indexes", "unindexed_fks"]
    assert report.is_healthy is True


# --- 2. One Check Fails and Another Succeeds ---


def test_partial_audit_preserves_successful_evidence_and_records_error():
    """Isolated check failure preserves valid evidence and records error traceability."""
    f1 = _make_dummy_finding("invalid_indexes", "orders.idx_orders_broken")
    report = build_audit_report(
        database="test_db",
        checks_executed=["invalid_indexes"],
        checks_requested=["invalid_indexes", "unindexed_fks"],
        checks_failed=["unindexed_fks"],
        findings=[f1],
        errors=["unindexed_fks: permission denied for relation pg_constraint"],
        is_partial=True,
    )
    assert report.is_partial is True
    assert len(report.findings) == 1
    assert report.findings[0].object_name == "orders.idx_orders_broken"
    assert report.checks_executed == ["invalid_indexes"]
    assert report.checks_failed == ["unindexed_fks"]
    assert len(report.errors) == 1
    assert "unindexed_fks" in report.errors[0]
    assert report.is_healthy is False


# --- 3. Failed Check with Zero Findings in Remaining Checks ---


def test_partial_audit_with_no_findings_is_not_healthy():
    """A partial audit with 0 findings cannot be interpreted as healthy."""
    report = build_audit_report(
        database="test_db",
        checks_executed=["hot"],
        checks_requested=["hot", "invalid"],
        checks_failed=["invalid"],
        findings=[],
        errors=["invalid: connection timeout during catalog query"],
        is_partial=True,
    )
    assert len(report.findings) == 0
    assert report.is_partial is True
    assert report.is_healthy is False


def test_cli_has_issues_treats_partial_report_as_unhealthy():
    """CLI has_issues() returns True for partial reports even without findings."""
    clean_report = DatabaseHealthReport()
    assert has_issues(clean_report) is False

    partial_report = DatabaseHealthReport(
        is_partial=True,
        checks_failed=["invalid"],
        errors=["invalid: catalog locked"],
    )
    assert has_issues(partial_report) is True


def test_cli_main_returns_non_zero_for_partial_audit_without_findings(capsys):
    """CLI exit code must not be 0 when audit is degraded/partial."""
    partial_report = DatabaseHealthReport(
        is_partial=True,
        checks_executed=["hot"],
        checks_failed=["invalid"],
        errors=["invalid: catalog read timeout"],
    )

    def factory(db_url, connect_timeout=10):
        class FakeAuditor:
            def run_audit(self, **kwargs):
                return partial_report

        return FakeAuditor()

    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/db"],
        auditor_factory=factory,
    )
    assert code == 2  # Degraded / issues exit code, never 0 (healthy)


# --- 4. Global Fatal Failure ---


def test_global_connection_failure_remains_fatal():
    """Global infrastructure / connection failures remain fatal exceptions."""
    auditor = PostgresHealthAuditor("postgresql://u:p@invalid_host:5432/db", connect_timeout=1)
    with pytest.raises(DatabaseConnectionError):
        auditor.run_audit()


# --- 5. Semantic Equivalence & Diff Comparison ---


def test_complete_and_partial_audits_are_not_semantically_equivalent():
    """Complete and partial audits with identical findings must not be equal."""
    f1 = _make_dummy_finding("invalid_indexes", "orders.idx_orders_broken")
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)

    report_complete = AuditReport(
        audit_id="audit_complete",
        database="test_db",
        server_version="16.4",
        observed_at=now,
        execution_metadata=ExecutionMetadata(),
        checks_executed=["invalid_indexes", "unindexed_fks"],
        checks_requested=["invalid_indexes", "unindexed_fks"],
        checks_failed=[],
        is_partial=False,
        has_critical_issues=True,
        findings=[f1],
        errors=[],
    )

    report_partial = AuditReport(
        audit_id="audit_partial",
        database="test_db",
        server_version="16.4",
        observed_at=now,
        execution_metadata=ExecutionMetadata(),
        checks_executed=["invalid_indexes"],
        checks_requested=["invalid_indexes", "unindexed_fks"],
        checks_failed=["unindexed_fks"],
        is_partial=True,
        has_critical_issues=True,
        findings=[f1],
        errors=["unindexed_fks: query failed"],
    )

    # They are not semantically equal
    assert report_complete != report_partial

    # Diff between them reflects partial audit state
    diff = compare_audit_reports(report_complete, report_partial)
    assert diff.is_partial is True


# --- 6. CLI PostgresHealthAuditor Resilient Isolation ---


def test_cli_auditor_isolates_failing_check_and_rolls_back():
    """PostgresHealthAuditor continues execution when one check fails."""
    auditor = PostgresHealthAuditor("postgresql://u:p@localhost:5432/db")

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    # Mock _audit_hot_and_fillfactor to succeed
    auditor._audit_hot_and_fillfactor = MagicMock(
        return_value=[HotUpdateIssue("users", 100, 10, 10.0, 100, True, 1000, "1 MB")]
    )
    # Mock _audit_redundant_indexes to fail
    auditor._audit_redundant_indexes = MagicMock(
        side_effect=Exception("relation pg_am does not exist")
    )
    # Mock _audit_invalid_indexes to succeed
    auditor._audit_invalid_indexes = MagicMock(
        return_value=[InvalidIndexIssue("orders", "idx_bad", "5 MB")]
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(audit_pg, "psycopg2", MagicMock(connect=MagicMock(return_value=mock_conn)))
        mp.setattr(audit_pg, "HAS_PSYCOPG2", True)

        report = auditor.run_audit(checks=["hot", "redundant", "invalid"])

    # 1. Successful findings preserved
    assert len(report.hot_issues) == 1
    assert len(report.invalid_indexes) == 1
    assert len(report.redundant_indexes) == 0

    # 2. Check tracking
    assert report.is_partial is True
    assert "hot" in report.checks_executed
    assert "invalid" in report.checks_executed
    assert "redundant" not in report.checks_executed
    assert "redundant" in report.checks_failed

    # 3. Error recorded with origin
    assert len(report.errors) == 1
    assert "redundant: relation pg_am does not exist" in report.errors[0]

    # 4. Transaction rolled back on error to keep cursor healthy
    mock_conn.rollback.assert_called_once()


# --- 7. MCP AsyncPostgresHealthAuditor Resilient Isolation ---


@pytest.mark.asyncio
async def test_mcp_auditor_isolates_failing_check():
    """AsyncPostgresHealthAuditor continues execution when one check fails."""
    auditor = AsyncPostgresHealthAuditor("postgresql://u:p@localhost:5432/db")

    mock_conn = MagicMock()
    mock_conn.close = AsyncMock()

    # Mock invalid_indexes to succeed
    auditor._audit_invalid_indexes = AsyncMock(
        return_value=[
            mcp_pg_auditor.InvalidIndexIssue(
                child_table="orders", invalid_index="idx_bad", index_size="5 MB"
            )
        ]
    )
    # Mock unindexed_fks to fail
    auditor._audit_unindexed_fks = AsyncMock(
        side_effect=Exception("relation pg_constraint does not exist")
    )

    with pytest.MonkeyPatch.context() as mp:
        async def fake_connect(*args, **kwargs):
            return mock_conn

        mp.setattr(mcp_pg_auditor.asyncpg, "connect", fake_connect)

        report = await auditor.run_full_audit(
            schemas=["public"],
            checks=[CheckName.INVALID_INDEXES.value, CheckName.UNINDEXED_FKS.value],
        )

    # 1. Successful findings preserved
    assert len(report.invalid_indexes) == 1
    assert len(report.unindexed_fks) == 0

    # 2. Check tracking
    assert report.is_partial is True
    assert CheckName.INVALID_INDEXES.value in report.checks_executed
    assert CheckName.UNINDEXED_FKS.value not in report.checks_executed
    assert CheckName.UNINDEXED_FKS.value in report.checks_failed

    # 3. Error recorded with origin
    assert len(report.errors) == 1
    expected_err = f"{CheckName.UNINDEXED_FKS.value}: relation pg_constraint does not exist"
    assert expected_err in report.errors[0]

    # 4. Conversion to canonical AuditReport preserves partial semantics
    canonical = report.to_audit_report()
    assert canonical.is_partial is True
    assert CheckName.UNINDEXED_FKS.value in canonical.checks_failed
    assert canonical.is_healthy is False
