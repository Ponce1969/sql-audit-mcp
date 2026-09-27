"""Tests for lock contention and blocking tree analysis (pg_locks)."""

from datetime import datetime, timezone

from sql_audit.application import (
    build_lock_contention_report,
    lock_report_to_findings,
    render_lock_report_text,
)
from sql_audit.domain import Severity


def test_lock_engine_empty_when_no_blocks():
    """Returns empty report with 0 blocked processes when no contention exists."""
    report = build_lock_contention_report([], database="prod_db")
    assert report.total_blocked_processes == 0
    assert report.distinct_root_blockers == 0
    assert len(report.trees) == 0

    text = render_lock_report_text(report)
    assert "0 blocked processes (clean)" in text


def test_lock_engine_multi_level_blocking_chain():
    """Correctly reconstructs transitive blocking chain: PID 4312 -> 4388 -> 4410 -> 4421."""
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)

    # Simulated row dataset from SQL_LOCK_CONTENTION
    rows = [
        {
            "blocked_pid": 4388,
            "blocked_user": "app_user",
            "blocked_app": "api_server",
            "blocked_client_addr": "10.0.0.12",
            "blocked_duration_sec": 85.0,
            "blocked_xact_age_sec": 85.0,
            "blocked_wait_event_type": "Lock",
            "blocked_wait_event": "transactionid",
            "blocked_state": "active",
            "blocked_query": "SELECT * FROM orders WHERE id = 1 FOR UPDATE;",
            "blocking_pid": 4312,
            "blocking_user": "worker_user",
            "blocking_app": "batch_job",
            "blocking_client_addr": "10.0.0.10",
            "blocking_duration_sec": 142.0,
            "blocking_xact_age_sec": 142.0,
            "blocking_state": "idle in transaction",
            "blocking_wait_event_type": "Client",
            "blocking_wait_event": "ClientRead",
            "blocking_query": "UPDATE orders SET status = 'processing' WHERE id = 1;",
        },
        {
            "blocked_pid": 4410,
            "blocked_user": "report_user",
            "blocked_app": "analytics",
            "blocked_client_addr": "10.0.0.15",
            "blocked_duration_sec": 50.0,
            "blocked_xact_age_sec": 50.0,
            "blocked_wait_event_type": "Lock",
            "blocked_wait_event": "relation",
            "blocked_state": "active",
            "blocked_query": "ALTER TABLE orders ADD COLUMN flags int;",
            "blocking_pid": 4388,
            "blocking_user": "app_user",
            "blocking_app": "api_server",
            "blocking_client_addr": "10.0.0.12",
            "blocking_duration_sec": 85.0,
            "blocking_xact_age_sec": 85.0,
            "blocking_state": "active",
            "blocking_wait_event_type": "Lock",
            "blocking_wait_event": "transactionid",
            "blocking_query": "SELECT * FROM orders WHERE id = 1 FOR UPDATE;",
        },
        {
            "blocked_pid": 4421,
            "blocked_user": "billing_user",
            "blocked_app": "invoicing",
            "blocked_client_addr": "10.0.0.18",
            "blocked_duration_sec": 20.0,
            "blocked_xact_age_sec": 20.0,
            "blocked_wait_event_type": "Lock",
            "blocked_wait_event": "relation",
            "blocked_state": "active",
            "blocked_query": "SELECT count(*) FROM orders;",
            "blocking_pid": 4410,
            "blocking_user": "report_user",
            "blocking_app": "analytics",
            "blocking_client_addr": "10.0.0.15",
            "blocking_duration_sec": 50.0,
            "blocking_xact_age_sec": 50.0,
            "blocking_state": "active",
            "blocking_wait_event_type": "Lock",
            "blocking_wait_event": "relation",
            "blocking_query": "ALTER TABLE orders ADD COLUMN flags int;",
        },
    ]

    report = build_lock_contention_report(rows, database="prod_db", observed_at=now)

    assert report.total_blocked_processes == 3
    assert report.distinct_root_blockers == 1
    assert len(report.trees) == 1

    tree = report.trees[0]
    assert tree.root_pid == 4312
    assert tree.total_blocked == 3
    assert set(tree.blocked_pids) == {4388, 4410, 4421}
    assert tree.max_blocked_duration_sec == 85.0

    # Validate findings generation
    findings = lock_report_to_findings(report, server_version="16.4")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.finding_id == "PG-LOCK-CONTENTION:PID_4312"
    assert finding.severity == Severity.CRITICAL  # idle in transaction & > 60s
    assert "idle in transaction" in finding.reason
    assert finding.evidence[0].evidence_id.startswith("sha256:")

    # Validate text visualization
    rendered = render_lock_report_text(report)
    assert "PID 4312 [worker_user@batch_job] (idle in transaction" in rendered
    assert "↓ blocks PID 4388" in rendered
    assert "↓ blocks PID 4410" in rendered
    assert "↓ blocks PID 4421" in rendered


def test_cli_locks_contention_flow(capsys):
    """CLI --locks returns exit code 3 when active lock contention is detected."""
    import json
    from unittest.mock import MagicMock

    import audit_pg

    rows = [
        {
            "blocked_pid": 200,
            "blocked_user": "u1",
            "blocked_app": "app",
            "blocked_client_addr": "127.0.0.1",
            "blocked_duration_sec": 15.0,
            "blocked_xact_age_sec": 15.0,
            "blocked_wait_event_type": "Lock",
            "blocked_wait_event": "transactionid",
            "blocked_state": "active",
            "blocked_query": "SELECT * FROM t FOR UPDATE;",
            "blocking_pid": 100,
            "blocking_user": "u2",
            "blocking_app": "app",
            "blocking_client_addr": "127.0.0.1",
            "blocking_duration_sec": 30.0,
            "blocking_xact_age_sec": 30.0,
            "blocking_state": "active",
            "blocking_wait_event_type": "Client",
            "blocking_wait_event": "ClientRead",
            "blocking_query": "UPDATE t SET x = 1;",
        }
    ]
    report = build_lock_contention_report(rows, database="testdb")

    def factory(db_url, connect_timeout=10):
        mock = MagicMock()
        mock.audit_locks.return_value = report
        return mock

    # 1. Text mode
    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/testdb", "--locks"],
        auditor_factory=factory,
    )
    assert code == 3
    out = capsys.readouterr().out
    assert "LOCK CONTENTION REPORT: testdb" in out
    assert "PID 100 [u2@app]" in out
    assert "↓ blocks PID 200" in out

    # 2. JSON mode
    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/testdb", "--locks", "--json"],
        auditor_factory=factory,
    )
    assert code == 3
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["total_blocked_processes"] == 1
    assert data["distinct_root_blockers"] == 1
    assert data["trees"][0]["root_pid"] == 100


def test_cli_locks_clean_flow(capsys):
    """CLI --locks returns exit code 0 when no processes are blocked."""
    from unittest.mock import MagicMock

    import audit_pg

    report = build_lock_contention_report([], database="cleandb")

    def factory(db_url, connect_timeout=10):
        mock = MagicMock()
        mock.audit_locks.return_value = report
        return mock

    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/cleandb", "--locks"],
        auditor_factory=factory,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "0 blocked processes (clean)" in out


async def test_mcp_pg_locks_tool(monkeypatch):
    """FastMCP pg_locks tool executes and returns LockContentionReport."""
    from unittest.mock import AsyncMock

    from mcp_pg_auditor import pg_locks

    report = build_lock_contention_report([], database="mcp_test")

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/mcp_test")
    monkeypatch.setattr(
        "mcp_pg_auditor.AsyncPostgresHealthAuditor.audit_locks",
        AsyncMock(return_value=report),
    )

    result = await pg_locks(db_alias="default")
    assert result.total_blocked_processes == 0
    assert result.distinct_root_blockers == 0
    assert len(result.trees) == 0


def test_lock_engine_cycle_detection_terminates_cleanly():
    """A closed dependency cycle (100 -> 200 -> 100) must terminate cleanly
    without RecursionError.
    """
    rows = [
        {
            "blocked_pid": 200,
            "blocked_user": "u2",
            "blocked_app": "app2",
            "blocked_client_addr": "127.0.0.1",
            "blocked_duration_sec": 10.0,
            "blocked_xact_age_sec": 10.0,
            "blocked_wait_event_type": "Lock",
            "blocked_wait_event": "transactionid",
            "blocked_state": "active",
            "blocked_query": "UPDATE t SET x = 1 WHERE id = 1;",
            "blocking_pid": 100,
            "blocking_user": "u1",
            "blocking_app": "app1",
            "blocking_client_addr": "127.0.0.1",
            "blocking_duration_sec": 20.0,
            "blocking_xact_age_sec": 20.0,
            "blocking_state": "active",
            "blocking_wait_event_type": "Lock",
            "blocking_wait_event": "transactionid",
            "blocking_query": "UPDATE t SET x = 2 WHERE id = 2;",
        },
        {
            "blocked_pid": 100,
            "blocked_user": "u1",
            "blocked_app": "app1",
            "blocked_client_addr": "127.0.0.1",
            "blocked_duration_sec": 20.0,
            "blocked_xact_age_sec": 20.0,
            "blocked_wait_event_type": "Lock",
            "blocked_wait_event": "transactionid",
            "blocked_state": "active",
            "blocked_query": "UPDATE t SET x = 2 WHERE id = 2;",
            "blocking_pid": 200,
            "blocking_user": "u2",
            "blocking_app": "app2",
            "blocking_client_addr": "127.0.0.1",
            "blocking_duration_sec": 10.0,
            "blocking_xact_age_sec": 10.0,
            "blocking_state": "active",
            "blocking_wait_event_type": "Lock",
            "blocking_wait_event": "transactionid",
            "blocking_query": "UPDATE t SET x = 1 WHERE id = 1;",
        },
    ]

    report = build_lock_contention_report(rows, database="testdb")

    assert len(report.trees) >= 1
    tree = report.trees[0]
    # Verify no infinite loop occurred and total_blocked avoids duplicate counting
    assert tree.total_blocked == 2
    assert set(tree.blocked_pids) == {100, 200}
    # Verify cycle is explicitly identified in chain_representation
    assert "cycle detected" in tree.chain_representation.lower()


def test_lock_engine_convergent_dag_preserves_both_branches():
    """A convergent DAG diamond (100 -> 200 -> 400 and 100 -> 300 -> 400)
    must preserve both branches without false cycle detection.
    """
    rows = [
        {
            "blocked_pid": 200,
            "blocked_user": "u2",
            "blocked_app": "app2",
            "blocked_client_addr": "127.0.0.1",
            "blocked_duration_sec": 15.0,
            "blocked_xact_age_sec": 15.0,
            "blocked_wait_event_type": "Lock",
            "blocked_wait_event": "relation",
            "blocked_state": "active",
            "blocked_query": "SELECT * FROM t2;",
            "blocking_pid": 100,
            "blocking_user": "root",
            "blocking_app": "app_root",
            "blocking_client_addr": "127.0.0.1",
            "blocking_duration_sec": 45.0,
            "blocking_xact_age_sec": 45.0,
            "blocking_state": "active",
            "blocking_wait_event_type": "Lock",
            "blocking_wait_event": "relation",
            "blocking_query": "ALTER TABLE t1 ADD COLUMN c int;",
        },
        {
            "blocked_pid": 300,
            "blocked_user": "u3",
            "blocked_app": "app3",
            "blocked_client_addr": "127.0.0.1",
            "blocked_duration_sec": 12.0,
            "blocked_xact_age_sec": 12.0,
            "blocked_wait_event_type": "Lock",
            "blocked_wait_event": "relation",
            "blocked_state": "active",
            "blocked_query": "SELECT * FROM t3;",
            "blocking_pid": 100,
            "blocking_user": "root",
            "blocking_app": "app_root",
            "blocking_client_addr": "127.0.0.1",
            "blocking_duration_sec": 45.0,
            "blocking_xact_age_sec": 45.0,
            "blocking_state": "active",
            "blocking_wait_event_type": "Lock",
            "blocking_wait_event": "relation",
            "blocking_query": "ALTER TABLE t1 ADD COLUMN c int;",
        },
        {
            "blocked_pid": 400,
            "blocked_user": "u4",
            "blocked_app": "app4",
            "blocked_client_addr": "127.0.0.1",
            "blocked_duration_sec": 5.0,
            "blocked_xact_age_sec": 5.0,
            "blocked_wait_event_type": "Lock",
            "blocked_wait_event": "tuple",
            "blocked_state": "active",
            "blocked_query": "SELECT * FROM t4;",
            "blocking_pid": 200,
            "blocking_user": "u2",
            "blocking_app": "app2",
            "blocking_client_addr": "127.0.0.1",
            "blocking_duration_sec": 15.0,
            "blocking_xact_age_sec": 15.0,
            "blocking_state": "active",
            "blocking_wait_event_type": "Lock",
            "blocking_wait_event": "relation",
            "blocking_query": "SELECT * FROM t2;",
        },
        {
            "blocked_pid": 400,
            "blocked_user": "u4",
            "blocked_app": "app4",
            "blocked_client_addr": "127.0.0.1",
            "blocked_duration_sec": 5.0,
            "blocked_xact_age_sec": 5.0,
            "blocked_wait_event_type": "Lock",
            "blocked_wait_event": "tuple",
            "blocked_state": "active",
            "blocked_query": "SELECT * FROM t4;",
            "blocking_pid": 300,
            "blocking_user": "u3",
            "blocking_app": "app3",
            "blocking_client_addr": "127.0.0.1",
            "blocking_duration_sec": 12.0,
            "blocking_xact_age_sec": 12.0,
            "blocking_state": "active",
            "blocking_wait_event_type": "Lock",
            "blocking_wait_event": "relation",
            "blocking_query": "SELECT * FROM t3;",
        },
    ]

    report = build_lock_contention_report(rows, database="testdb")

    assert len(report.trees) == 1
    tree = report.trees[0]
    assert tree.root_pid == 100
    # Distinct blocked processes in diamond: 200, 300, 400
    assert tree.total_blocked == 3
    assert set(tree.blocked_pids) == {200, 300, 400}
    # Both paths leading to 400 must be present in chain representation
    assert tree.chain_representation.count("PID 400") == 2
    # Neither branch should be misclassified as a cycle
    assert "cycle detected" not in tree.chain_representation.lower()
