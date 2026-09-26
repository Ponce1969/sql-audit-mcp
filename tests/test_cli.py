import json

import audit_pg
from audit_pg import DatabaseHealthReport, HotUpdateIssue, RedundantIndexIssue


def make_auditor_factory(report):
    def factory(db_url, connect_timeout=10):
        class FakeAuditor:
            def run_audit(self, **kwargs):
                return report

        return FakeAuditor()

    return factory


def test_parser_new_flags():
    parser = audit_pg.build_parser()
    args = parser.parse_args(
        [
            "--checks",
            "redundant,hot",
            "--schema",
            "public",
            "--schema",
            "analytics",
            "--min-size",
            "1024",
            "--json",
            "--env-file",
            ".env.prod",
            "--timeout",
            "5",
        ]
    )
    assert args.checks == "redundant,hot"
    assert args.schema == ["public", "analytics"]
    assert args.min_size == 1024
    assert args.json is True
    assert args.env_file == ".env.prod"
    assert args.timeout == 5


def test_parser_defaults():
    args = audit_pg.build_parser().parse_args([])
    assert args.checks is None
    assert args.schema is None
    assert args.min_size == 0
    assert args.json is False
    assert args.quiet is False
    assert args.env_file is None
    assert args.timeout == 10
    assert args.min_hot_ratio == 30.0
    assert args.min_updates == 50
    assert args.max_rw_ratio == 0.05
    assert args.min_table_rows == 10000


def test_parser_quiet():
    args = audit_pg.build_parser().parse_args(["--quiet"])
    assert args.quiet is True


def test_exit_0_when_clean():
    report = DatabaseHealthReport()
    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/db"],
        auditor_factory=make_auditor_factory(report),
    )
    assert code == 0


def test_exit_2_when_issues():
    report = DatabaseHealthReport(
        hot_issues=[HotUpdateIssue("orders", 100, 10, 10.0, 100, True, 50000, "12 MB")]
    )
    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/db"],
        auditor_factory=make_auditor_factory(report),
    )
    assert code == 2


def test_exit_1_when_no_config(capsys, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("PGPORT", raising=False)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setattr(audit_pg, "find_dotenv", lambda usecwd=True: "")
    code = audit_pg.main([])
    assert code == 1
    assert "no database configuration" in capsys.readouterr().err


def test_json_output_is_valid_and_redacted(capsys):
    report = DatabaseHealthReport(
        hot_issues=[HotUpdateIssue("orders", 100, 50, 50.0, 100, True, 50000, "12 MB")],
        redundant_indexes=[
            RedundantIndexIssue("users", "idx_users_a", "16 kB", "idx_users_b", "def a", "def b")
        ],
    )
    code = audit_pg.main(
        ["--url", "postgresql://dbadmin:supersecret@db.example.com:5432/mydb", "--json"],
        auditor_factory=make_auditor_factory(report),
    )
    assert code == 2
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["database"] == "db.example.com"
    assert "supersecret" not in out
    assert data["checks"] == [
        "redundant", "hot", "low-usage", "invalid", "unindexed-fks", "dead-tuples"
    ]
    assert data["summary"] == {
        "redundant_indexes": 1,
        "hot_issues": 1,
        "low_usage_indexes": 0,
        "invalid_indexes": 0,
        "unindexed_fks": 0,
        "autovacuum_dead_tuples": 0,
    }
    assert set(data["issues"]) == {
        "redundant", "hot", "low-usage", "invalid", "unindexed-fks", "dead-tuples"
    }


def test_quiet_output_shape(capsys):
    report = DatabaseHealthReport(
        hot_issues=[HotUpdateIssue("orders", 100, 10, 10.0, 100, True, 50000, "12 MB")]
    )
    code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/db", "--quiet"],
        auditor_factory=make_auditor_factory(report),
    )
    assert code == 2
    out = capsys.readouterr().out
    assert "redundant_indexes: 0" in out
    assert "hot_issues: 1" in out
    assert "low_usage_indexes: 0" in out
    assert "HEALTH REPORT" not in out


def test_flags_flow_to_auditor():
    seen = {}

    def factory(db_url, connect_timeout=10):
        class FakeAuditor:
            def run_audit(self, **kwargs):
                seen.update(kwargs)
                return DatabaseHealthReport()

        return FakeAuditor()

    code = audit_pg.main(
        [
            "--url",
            "postgresql://u:p@localhost/db",
            "--checks",
            "hot",
            "--schema",
            "public,analytics",
            "--min-size",
            "512",
        ],
        auditor_factory=factory,
    )
    assert code == 0
    assert seen["checks"] == ["hot"]
    assert seen["schemas"] == ["public", "analytics"]
    assert seen["min_size_bytes"] == 512
    assert seen["min_table_rows"] == 10000


def test_invalid_check_exits_1(capsys):
    code = audit_pg.main(["--url", "postgresql://u:p@localhost/db", "--checks", "bogus"])
    assert code == 1
    assert "bogus" in capsys.readouterr().err


def test_min_table_rows_negative_exits_1(capsys):
    code = audit_pg.main(["--url", "postgresql://u:p@localhost/db", "--min-table-rows", "-1"])
    assert code == 1
    assert "must be >= 0" in capsys.readouterr().err


def test_connection_error_exits_1(capsys):
    def factory(db_url, connect_timeout=10):
        class FakeAuditor:
            def run_audit(self, **kwargs):
                raise audit_pg.DatabaseConnectionError("connection refused")

        return FakeAuditor()

    code = audit_pg.main(["--url", "postgresql://u:p@localhost/db"], auditor_factory=factory)
    assert code == 1
    assert "Connection failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Phase 1: SQL divergence regression tests (tasks 1.1, 1.3, 1.5)
# Each test captures the actual SQL string sent to the cursor and asserts
# that the canonical query pattern from mcp_pg_auditor.py is present.
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Captures SQL strings sent via execute(); returns [] from fetchall()."""

    def __init__(self, store: dict[str, str], key: str):
        self._store = store
        self._key = key

    def execute(self, sql: str, params=None) -> None:
        self._store[self._key] = sql

    def fetchall(self) -> list:
        return []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _FakeConn:
    def __init__(self, store: dict[str, str], key: str):
        self._store = store
        self._key = key

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._store, self._key)

    def close(self):
        pass


def _patch_psycopg2_connect(monkeypatch, key: str) -> dict[str, str]:
    """Returns the SQL store dict; patches psycopg2.connect to use FakeConn."""
    store: dict[str, str] = {}
    monkeypatch.setattr(audit_pg.psycopg2, "connect", lambda *a, **kw: _FakeConn(store, key))
    monkeypatch.setattr(audit_pg, "HAS_PSYCOPG2", True)
    return store


def test_hot_query_uses_pg_options_to_table(monkeypatch):
    """1.1 RED → 1.2 GREEN: fillfactor subquery must use pg_options_to_table."""
    store = _patch_psycopg2_connect(monkeypatch, "hot")
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    auditor.run_audit(checks=["hot"])
    assert "pg_options_to_table" in store["hot"], (
        "Expected pg_options_to_table in HOT query; got split_part/unnest instead"
    )


def test_redundant_query_guards_btree_and_valid_indexes(monkeypatch):
    """1.3 RED → 1.4 GREEN: redundant CTE must filter btree-only and valid indexes."""
    store = _patch_psycopg2_connect(monkeypatch, "redundant")
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    auditor.run_audit(checks=["redundant"])
    sql = store["redundant"]
    assert "am.amname" in sql, "Expected am.amname join in redundant CTE"
    assert "btree" in sql, "Expected btree filter in redundant CTE"
    assert "indisvalid" in sql, "Expected indisvalid guard in redundant CTE"


def test_low_usage_query_uses_threshold_1000(monkeypatch):
    """1.5 RED → 1.6 GREEN: low-usage writes threshold must be 1000, not 100."""
    store = _patch_psycopg2_connect(monkeypatch, "low-usage")
    auditor = audit_pg.PostgresHealthAuditor("postgresql://u:p@h/db")
    auditor.run_audit(checks=["low-usage"])
    sql = store["low-usage"]
    # The threshold must be > 1000 and must NOT be the old > 100 pattern
    assert "> 1000" in sql or "> %s" in sql, (
        "Expected writes threshold of 1000 in low-usage query"
    )
    # Verify the old threshold is gone (literal 100 without 1000 prefix)
    import re
    assert not re.search(r'>\s*100\b(?!0)', sql), (
        "Old threshold > 100 still present in low-usage query"
    )
