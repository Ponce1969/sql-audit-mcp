"""Tests for T-08: Defensive Session Timeouts (DEFENSIVE_SESSION_TIMEOUTS).

Verifies that:
1. Canonical defaults for statement_timeout and lock_timeout are centralized in sql_audit.domain.
2. CLI and MCP use identical contractual defaults (no divergent transport defaults).
3. CLI applies timeouts at session level via connection startup options.
4. MCP applies timeouts at session level via server_settings.
5. Isolated query/lock timeout errors integrate with T-03 as partial/degraded audit failures.
6. Valid findings from non-timing-out checks are preserved.
7. Session connection / configuration failures remain fatal global errors.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mcp_pg_auditor
from audit_pg import DatabaseConnectionError, HotUpdateIssue, PostgresHealthAuditor
from mcp_pg_auditor import AsyncPostgresHealthAuditor, CheckName
from sql_audit.domain import (
    DEFAULT_LOCK_TIMEOUT_MS,
    DEFAULT_STATEMENT_TIMEOUT_MS,
)


def test_canonical_session_timeout_defaults_centralized():
    """Validates that statement and lock timeout defaults are defined in domain."""
    assert DEFAULT_STATEMENT_TIMEOUT_MS == 15_000
    assert DEFAULT_LOCK_TIMEOUT_MS == 3_000


def test_cli_auditor_defaults_match_domain_constants():
    """PostgresHealthAuditor defaults match domain constants."""
    auditor = PostgresHealthAuditor("postgresql://u:p@localhost/db")
    assert auditor.statement_timeout_ms == DEFAULT_STATEMENT_TIMEOUT_MS
    assert auditor.lock_timeout_ms == DEFAULT_LOCK_TIMEOUT_MS


def test_mcp_auditor_defaults_match_domain_constants():
    """AsyncPostgresHealthAuditor defaults match domain constants (no divergence)."""
    auditor = AsyncPostgresHealthAuditor("postgresql://u:p@localhost/db")
    assert auditor.statement_timeout_ms == DEFAULT_STATEMENT_TIMEOUT_MS
    assert auditor.lock_timeout_ms == DEFAULT_LOCK_TIMEOUT_MS


def test_cli_applies_session_timeouts_in_connection_options():
    """CLI connects with -c statement_timeout and -c lock_timeout in libpq options."""
    auditor = PostgresHealthAuditor("postgresql://u:p@localhost/db")
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    with patch("audit_pg.psycopg2.connect", return_value=mock_conn) as mock_connect:
        auditor._audit_invalid_indexes = MagicMock(return_value=[])
        auditor.run_audit(checks=["invalid"])

    mock_connect.assert_called_once()
    kwargs = mock_connect.call_args.kwargs
    assert "options" in kwargs
    assert f"-c statement_timeout={DEFAULT_STATEMENT_TIMEOUT_MS}" in kwargs["options"]
    assert f"-c lock_timeout={DEFAULT_LOCK_TIMEOUT_MS}" in kwargs["options"]


@pytest.mark.asyncio
async def test_mcp_applies_session_timeouts_in_server_settings():
    """MCP connects with server_settings dictionary for session-level timeouts."""
    auditor = AsyncPostgresHealthAuditor("postgresql://u:p@localhost/db")
    mock_conn = AsyncMock()
    mock_conn.close = AsyncMock()

    with patch("mcp_pg_auditor.asyncpg.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn
        auditor._audit_invalid_indexes = AsyncMock(return_value=[])
        await auditor.run_full_audit(schemas=["public"], checks=[CheckName.INVALID_INDEXES.value])

    mock_connect.assert_called_once()
    kwargs = mock_connect.call_args.kwargs
    assert "server_settings" in kwargs
    settings = kwargs["server_settings"]
    assert settings["statement_timeout"] == str(DEFAULT_STATEMENT_TIMEOUT_MS)
    assert settings["lock_timeout"] == str(DEFAULT_LOCK_TIMEOUT_MS)


def test_cli_isolated_statement_timeout_integrates_with_t03():
    """A statement_timeout during a check in CLI produces a partial audit
    and preserves evidence of other checks.
    """
    auditor = PostgresHealthAuditor("postgresql://u:p@localhost/db")
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    # 'hot' succeeds with 1 finding
    auditor._audit_hot_and_fillfactor = MagicMock(
        return_value=[HotUpdateIssue("users", 100, 10, 10.0, 100, True, 1000, "1 MB")]
    )
    # 'redundant' fails with statement timeout
    auditor._audit_redundant_indexes = MagicMock(
        side_effect=Exception("canceling statement due to statement timeout")
    )

    with patch("audit_pg.psycopg2.connect", return_value=mock_conn):
        report = auditor.run_audit(checks=["hot", "redundant"])

    # Successful check evidence is preserved
    assert len(report.hot_issues) == 1
    # Check tracking
    assert report.is_partial is True
    assert "hot" in report.checks_executed
    assert "redundant" in report.checks_failed
    # Error registered with check origin
    assert len(report.errors) == 1
    assert "redundant: canceling statement due to statement timeout" in report.errors[0]
    # Rollback invoked to reset transaction
    mock_conn.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_mcp_isolated_lock_timeout_integrates_with_t03():
    """A lock_timeout during a check in MCP produces a partial audit and preserves other checks."""
    auditor = AsyncPostgresHealthAuditor("postgresql://u:p@localhost/db")
    mock_conn = AsyncMock()
    mock_conn.close = AsyncMock()

    # 'invalid' succeeds with 1 finding
    auditor._audit_invalid_indexes = AsyncMock(
        return_value=[
            mcp_pg_auditor.InvalidIndexIssue(
                child_table="orders", invalid_index="idx_bad", index_size="5 MB"
            )
        ]
    )
    # 'unindexed_fks' fails with lock timeout
    auditor._audit_unindexed_fks = AsyncMock(
        side_effect=Exception("canceling statement due to lock timeout")
    )

    with patch("mcp_pg_auditor.asyncpg.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn
        report = await auditor.run_full_audit(
            schemas=["public"],
            checks=[CheckName.INVALID_INDEXES.value, CheckName.UNINDEXED_FKS.value],
        )

    # Successful check evidence is preserved
    assert len(report.invalid_indexes) == 1
    # Check tracking
    assert report.is_partial is True
    assert CheckName.INVALID_INDEXES.value in report.checks_executed
    assert CheckName.UNINDEXED_FKS.value in report.checks_failed
    # Error registered
    assert len(report.errors) == 1
    assert "canceling statement due to lock timeout" in report.errors[0]


def test_cli_session_connection_failure_remains_fatal():
    """Connection failure during connect / session setup raises DatabaseConnectionError."""
    auditor = PostgresHealthAuditor("postgresql://invalid:5432/db")
    with patch("audit_pg.psycopg2.connect", side_effect=Exception("could not connect to server")):
        with pytest.raises(DatabaseConnectionError):
            auditor.run_audit()
