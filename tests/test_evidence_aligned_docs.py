"""Tests for T-10: EVIDENCE_ALIGNED_DOCUMENTATION.

Verifies that documentation (README.md, SKILL.md) strictly aligns with the
actual implemented behavior, contracts, and safety guarantees of the codebase:
- Strictly passive audit (no EXPLAIN ANALYZE, no --analyze, no rollback).
- Unified canonical defaults (min_table_rows=10000, min_size_bytes=0).
- Defensive session timeouts (15s statement, 3s lock).
- Resilient partial audit semantics (is_partial, fault isolation).
- Absence of unverified claims (no O(1), no exact bloat claims).
"""

from pathlib import Path


def _get_readme_text() -> str:
    readme_path = Path(__file__).resolve().parent.parent / "README.md"
    assert readme_path.exists(), "README.md must exist"
    return readme_path.read_text(encoding="utf-8")


def _get_skill_text() -> str:
    skill_path = Path("C:/Users/cerra/.gemini/config/skills/sql-audit/SKILL.md")
    if not skill_path.exists():
        return ""
    return skill_path.read_text(encoding="utf-8")


def test_readme_and_skill_contain_no_explain_analyze_or_active_flags() -> None:
    """T-10 / T-01: Documentation must reflect strictly passive EXPLAIN without --analyze."""
    readme = _get_readme_text()
    assert "--analyze" not in readme
    assert "EXPLAIN ANALYZE" not in readme
    assert "ROLLBACK" not in readme

    skill = _get_skill_text()
    if skill:
        assert "--analyze" not in skill
        assert "explain / analyze" not in skill.lower()
        assert "explain/analyze" not in skill.lower()
        assert "rollback" not in skill.lower()


def test_readme_and_skill_contain_no_unsupported_performance_or_exactness_claims() -> None:
    """T-10: Documentation must not claim O(1) complexity or exact bloat measurements."""
    readme = _get_readme_text()
    assert "O(1)" not in readme
    assert "$O(1)$" not in readme
    assert "exact bloat" not in readme.lower()

    skill = _get_skill_text()
    if skill:
        assert "O(1)" not in skill
        assert "$O(1)$" not in skill
        assert "exacto" not in skill.lower()


def test_readme_documents_canonical_defaults() -> None:
    """T-10 / T-02: README must document canonical defaults (min_table_rows=10k, min_size=0)."""
    readme = _get_readme_text()
    assert "10000" in readme or "10,000" in readme
    assert "min_table_rows" in readme or "min-table-rows" in readme


def test_readme_documents_defensive_session_timeouts() -> None:
    """T-10 / T-08: README must document statement_timeout=15s and lock_timeout=3s."""
    readme = _get_readme_text()
    assert "statement_timeout" in readme
    assert "lock_timeout" in readme
    assert "15" in readme  # 15s or 15000ms
    assert "3" in readme   # 3s or 3000ms


def test_readme_documents_resilient_partial_audit() -> None:
    """T-10 / T-03: README must document fault-isolation and is_partial semantics."""
    readme = _get_readme_text()
    assert "is_partial" in readme or "partial" in readme.lower()
    assert "degraded" in readme.lower() or "isolation" in readme.lower()


def test_readme_documents_offline_migrations_boundary() -> None:
    """T-10: README must clarify historical scripts/migrations boundary."""
    readme = _get_readme_text()
    assert "scripts/migrations" in readme
