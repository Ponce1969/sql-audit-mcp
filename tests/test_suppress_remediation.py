"""Tests for T-09: SUPPRESS_EXECUTABLE_REMEDIATION.

Ensures that the PostgreSQL health auditor acts as a passive diagnostic tool:
- Emits diagnostic findings, explanations of risks, and operational considerations.
- Never emits executable SQL/DDL/DML or operational commands
  (DROP, CREATE, ALTER, VACUUM FULL, REINDEX, pg_terminate_backend).
"""

import re

from audit_pg import (
    DatabaseHealthReport,
    DeadTuplesIssue,
    HotUpdateIssue,
    InvalidIndexIssue,
    LowUsageIndexIssue,
    RedundantIndexIssue,
    UnindexedFKIssue,
    render_text,
)
from sql_audit.application.bloat_service import (
    bloat_report_to_findings,
    build_bloat_report,
    render_bloat_report_text,
)
from sql_audit.application.lock_service import (
    LockContentionReport,
    LockProcess,
    LockTree,
    lock_report_to_findings,
    render_lock_report_text,
)

GLOBAL_FORBIDDEN_PATTERNS = [
    r"\bDROP\s+INDEX\b",
    r"\bALTER\s+TABLE\b",
    r"\bVACUUM\s+FULL\b",
    r"\bREINDEX\b",
    r"\bpg_terminate_backend\b",
    r"\bpg_cancel_backend\b",
    r"\bpg_repack\b",
]

CONSIDERATIONS_FORBIDDEN_PATTERNS = [
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b",
    r"\bDROP\s+INDEX\b",
    r"\bALTER\s+TABLE\b",
    r"\bVACUUM\b",
    r"\bREINDEX\b",
    r"\bpg_terminate_backend\b",
    r"\bpg_cancel_backend\b",
    r"\bpg_repack\b",
]


def _assert_no_executable_commands(text: str) -> None:
    # 1. Global forbidden patterns across all output (must never appear anywhere)
    for pattern in GLOBAL_FORBIDDEN_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        assert match is None, (
            f"Found forbidden executable command matching '{pattern}' in output:\n{text}"
        )

    # 2. Considerations and suggestion lines must never contain actionable DDL/DML
    for line in text.splitlines():
        trimmed = line.strip()
        if trimmed.startswith(("- Considerations:", "- Suggestion:")):
            for pattern in CONSIDERATIONS_FORBIDDEN_PATTERNS:
                match = re.search(pattern, trimmed, re.IGNORECASE)
                assert match is None, (
                    f"Found forbidden DDL in line matching '{pattern}':\n{trimmed}"
                )


def test_render_text_index_findings_contain_no_executable_commands() -> None:
    """T-09: Index findings in render_text must provide considerations without executable DDL."""
    report = DatabaseHealthReport(
        invalid_indexes=[
            InvalidIndexIssue("orders", "idx_orders_bad", "10 MB")
        ],
        unindexed_fks=[
            UnindexedFKIssue(
                "order_items", "fk_order", "orders", "FOREIGN KEY (o_id) REFERENCES orders(id)"
            )
        ],
        redundant_indexes=[
            RedundantIndexIssue(
                table_name="orders",
                redundant_index="idx_redundant",
                redundant_size="8 MB",
                covering_index="idx_covering",
                redundant_def="CREATE INDEX idx_redundant ON orders(a)",
                covering_def="CREATE INDEX idx_covering ON orders(a, b)",
            )
        ],
        low_usage_indexes=[
            LowUsageIndexIssue("events", "idx_events_x", "32 kB", 3, 5000, 0.0006, 50000, "12 MB")
        ],
    )
    text = render_text(report)

    # 1. No executable commands
    _assert_no_executable_commands(text)

    # 2. Preserves diagnostic and contextual information
    assert "orders" in text
    assert "idx_orders_bad" in text
    assert "order_items -> orders" in text
    assert "fk_order" in text
    assert "idx_redundant" in text
    assert "idx_covering" in text
    assert "idx_events_x" in text
    assert "Considerations:" in text


def test_render_text_hot_and_vacuum_contain_no_executable_commands() -> None:
    """T-09: HOT updates and autovacuum lag findings must not emit ALTER TABLE or VACUUM FULL."""
    report = DatabaseHealthReport(
        autovacuum_dead_tuples=[
            DeadTuplesIssue("logs", 20000, 100000, 16.67, None, None)
        ],
        hot_issues=[
            HotUpdateIssue(
                table_name="orders",
                total_updates=100,
                hot_updates=10,
                hot_ratio_pct=10.0,
                fillfactor=100,
                fillfactor_warning=True,
                table_rows=50000,
                table_size="12 MB",
            )
        ],
    )
    text = render_text(report)

    # 1. No executable commands
    _assert_no_executable_commands(text)

    # 2. Preserves diagnostic information
    assert "logs" in text
    assert "20,000" in text
    assert "orders" in text
    assert "10.0%" in text
    assert "Considerations:" in text


def test_bloat_service_findings_and_render_have_no_executable_commands() -> None:
    """T-09: Physical bloat findings and text summary must not prescribe VACUUM FULL or REINDEX."""
    report = build_bloat_report(
        table_rows=[
            {
                "schema_name": "public",
                "table_name": "users",
                "table_size_bytes": 100_000_000,
                "expected_size_bytes": 50_000_000,
                "bloat_bytes": 50_000_000,
                "bloat_ratio_pct": 50.0,
            }
        ],
        index_rows=[
            {
                "schema_name": "public",
                "table_name": "users",
                "index_name": "idx_users_email",
                "index_size_bytes": 60_000_000,
                "expected_size_bytes": 20_000_000,
                "bloat_bytes": 40_000_000,
                "bloat_ratio_pct": 66.7,
            }
        ],
    )

    rendered = render_bloat_report_text(report)
    _assert_no_executable_commands(rendered)
    assert "public.users" in rendered
    assert "idx_users_email" in rendered

    findings = bloat_report_to_findings(report)
    for f in findings:
        _assert_no_executable_commands(f.reason)


def test_lock_service_findings_and_render_have_no_termination_commands() -> None:
    """T-09: Lock contention findings and report must not emit pg_terminate_backend or kill."""
    from datetime import datetime, timezone

    from sql_audit.domain.locks import BlockingEdge

    proc_root = LockProcess(
        pid=100,
        user="app",
        application_name="worker",
        state="idle in transaction",
        xact_age_sec=45.0,
        query="UPDATE accounts SET balance = 0",
    )
    proc_blocked = LockProcess(
        pid=101,
        user="app",
        application_name="web",
        state="active",
        xact_age_sec=30.0,
        query="SELECT * FROM accounts WHERE id = 1 FOR UPDATE",
    )
    tree = LockTree(
        root_pid=100,
        root_process=proc_root,
        total_blocked=1,
        blocked_pids=[101],
        max_blocked_duration_sec=30.0,
        chain_representation="PID 100 (idle in transaction) -> PID 101 (blocked)",
    )
    report = LockContentionReport(
        observed_at=datetime.now(timezone.utc),
        database="testdb",
        total_blocked_processes=1,
        distinct_root_blockers=1,
        trees=[tree],
        raw_edges=[
            BlockingEdge(
                blocking_pid=100,
                blocked_pid=101,
                blocking_process=proc_root,
                blocked_process=proc_blocked,
            )
        ],
    )

    rendered = render_lock_report_text(report)
    _assert_no_executable_commands(rendered)
    assert "PID 100" in rendered

    findings = lock_report_to_findings(report)
    for f in findings:
        _assert_no_executable_commands(f.reason)


def test_cli_and_mcp_findings_maintain_passive_remediation_contract() -> None:
    """T-09: Canonical findings generated from CLI or MCP mapping never contain executable DDL."""
    from sql_audit.application.mapping import convert_legacy_report_to_audit_report

    cli_report = DatabaseHealthReport(
        invalid_indexes=[InvalidIndexIssue("orders", "idx_bad", "10 MB")],
        redundant_indexes=[
            RedundantIndexIssue("orders", "idx_a", "idx_b", "def1", "def2", "5 MB")
        ],
    )
    canon_cli = convert_legacy_report_to_audit_report(
        cli_report, database="testdb", checks=["invalid_indexes", "redundant_indexes"]
    )
    for f in canon_cli.findings:
        _assert_no_executable_commands(f.reason)

    import mcp_pg_auditor

    mcp_report = mcp_pg_auditor.PostgresHealthReport(
        database_alias="testdb",
        schemas_audited=["public"],
        invalid_indexes=[
            mcp_pg_auditor.InvalidIndexIssue(
                child_table="orders", invalid_index="idx_bad", index_size="10 MB"
            )
        ],
        redundant_indexes=[
            mcp_pg_auditor.RedundantIndexIssue(
                table_name="orders",
                redundant_index="idx_a",
                covering_index="idx_b",
                redundant_size="5 MB",
                redundant_def="CREATE INDEX idx_a ON orders(a)",
                covering_def="CREATE INDEX idx_b ON orders(a, b)",
            )
        ],
    )
    canon_mcp = convert_legacy_report_to_audit_report(
        mcp_report, database="testdb", checks=["invalid_indexes", "redundant_indexes"]
    )
    for f in canon_mcp.findings:
        _assert_no_executable_commands(f.reason)


def test_readme_sample_output_contains_no_executable_remediation() -> None:
    """T-09: README sample outputs and descriptions must not contain executable DDL suggestions."""
    from pathlib import Path

    readme_path = Path(__file__).resolve().parent.parent / "README.md"
    readme_text = readme_path.read_text(encoding="utf-8")

    # The sample output section in README must not suggest DROP INDEX CONCURRENTLY
    assert "Suggestion: Consider DROP INDEX" not in readme_text
    assert "Zero-Downtime Remediation" not in readme_text


def test_skill_contains_no_prescribed_executable_remediation_commands() -> None:
    """T-09: SKILL.md remediation section must not prescribe executable SQL commands."""
    from pathlib import Path

    skill_path = Path("C:/Users/cerra/.gemini/config/skills/sql-audit/SKILL.md")
    if not skill_path.exists():
        return

    skill_text = skill_path.read_text(encoding="utf-8")
    assert "SELECT pg_terminate_backend(" not in skill_text
    assert "SELECT pg_cancel_backend(" not in skill_text
    assert "DROP INDEX CONCURRENTLY IF EXISTS" not in skill_text
    assert "REINDEX INDEX CONCURRENTLY" not in skill_text

