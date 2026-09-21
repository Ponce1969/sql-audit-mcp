import audit_pg


def test_cli_url_has_highest_priority(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://env.example/db")
    assert audit_pg.resolve_db_url("postgresql://cli.example/db", None) == (
        "postgresql://cli.example/db"
    )


def test_env_file_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("PGUSER", raising=False)
    env_file = tmp_path / "explicit.env"
    env_file.write_text("DATABASE_URL=postgresql://explicit.example/mydb\n")
    assert audit_pg.resolve_db_url(None, str(env_file)) == "postgresql://explicit.example/mydb"


def test_env_file_falls_through_to_walkup(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    explicit = tmp_path / "empty.env"
    explicit.write_text("# no DATABASE_URL here\n")
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://walkup.example/mydb\n")
    monkeypatch.chdir(tmp_path)
    assert audit_pg.resolve_db_url(None, str(explicit)) == "postgresql://walkup.example/mydb"


def test_walkup_env_discovery(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("PGUSER", raising=False)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://walkup.example/mydb\n")
    monkeypatch.chdir(nested)
    assert audit_pg.resolve_db_url(None, None) == "postgresql://walkup.example/mydb"


def test_libpq_env_fallback(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(audit_pg, "find_dotenv", lambda usecwd=True: "")
    monkeypatch.setenv("PGHOST", "db.example.com")
    monkeypatch.setenv("PGPORT", "5433")
    monkeypatch.setenv("PGUSER", "alice")
    monkeypatch.setenv("PGPASSWORD", "s3cret")
    monkeypatch.setenv("PGDATABASE", "mydb")
    assert audit_pg.resolve_db_url(None, None) == (
        "postgresql://alice:s3cret@db.example.com:5433/mydb"
    )


def test_libpq_env_minimal(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setattr(audit_pg, "find_dotenv", lambda usecwd=True: "")
    monkeypatch.setenv("PGHOST", "localhost")
    monkeypatch.setenv("PGDATABASE", "appdb")
    assert audit_pg.resolve_db_url(None, None) == "postgresql://localhost:5432/appdb"


def test_postgres_env_fallback_docker_compose(monkeypatch):
    """docker-compose style POSTGRES_* vars must work as a fallback (real-world .env pattern)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.setattr(audit_pg, "find_dotenv", lambda usecwd=True: "")
    monkeypatch.setenv("POSTGRES_DB", "familiar")
    monkeypatch.setenv("POSTGRES_USER", "app_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:word")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    assert audit_pg.resolve_db_url(None, None) == (
        "postgresql://app_user:p%40ss%3Aword@localhost:5432/familiar"
    )


def test_libpq_wins_over_postgres_env(monkeypatch):
    """Standard libpq PG* vars take precedence over the POSTGRES_* fallback."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(audit_pg, "find_dotenv", lambda usecwd=True: "")
    monkeypatch.setenv("PGHOST", "db.std")
    monkeypatch.setenv("PGDATABASE", "stddb")
    monkeypatch.setenv("POSTGRES_DB", "composedb")
    monkeypatch.setenv("POSTGRES_HOST", "db.compose")
    assert audit_pg.resolve_db_url(None, None) == "postgresql://db.std:5432/stddb"


def test_missing_config_returns_none(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("PGPORT", raising=False)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.delenv("POSTGRES_USER", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    monkeypatch.setattr(audit_pg, "find_dotenv", lambda usecwd=True: "")
    assert audit_pg.resolve_db_url(None, None) is None
