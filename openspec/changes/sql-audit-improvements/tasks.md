# Tasks: SQL Audit Improvements

> **Artifact store**: engram (primary) | filesystem fallback at this path
> **Project**: Herraminetas_Sql
> **Topic key**: `sdd/sql-audit-improvements/tasks`
> **TDD mode**: ACTIVE — test runner: `uv run pytest tests/`

---

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | 620–720 (additions + deletions) |
| 400-line budget risk | **High** |
| Chained PRs recommended | **Yes** |
| Suggested split | PR 1 → Foundation + Query Fixes + CLI Checks · PR 2 → MCP Tests + Config Fixes |
| Delivery strategy | ask-on-risk |
| Chain strategy | pending (ask user) |

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: pending
400-line budget risk: High

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|------|------|-----------|-------|
| 1 | Foundation, query fixes, CLI critical checks + exit code | PR 1 | Targets main; all changes in `audit_pg.py`; tests in `tests/test_cli.py` |
| 2 | MCP test suite + config fixes (`mcp_pg_auditor.py`, `pyproject.toml`) | PR 2 | Targets main or PR 1 branch; isolated async test surface |

---

## Phase 0: Foundation — New Types & Report Extension

> No behavior change. Pure structural additions. Must land before any other phase.

- [x] 0.1 Add `InvalidIndexIssue`, `UnindexedFKIssue`, `DeadTuplesIssue` frozen dataclasses to `audit_pg.py` (after existing issue dataclasses; import `datetime` at top)
- [x] 0.2 Extend `DatabaseHealthReport` in `audit_pg.py`: add `invalid_indexes`, `unindexed_fks`, `autovacuum_dead_tuples` fields (all `list[...] = field(default_factory=list)`)
- [x] 0.3 Add `has_critical_issues` `@property` to `DatabaseHealthReport` (returns `bool(self.invalid_indexes or self.unindexed_fks or self.autovacuum_dead_tuples)`)
- [x] 0.4 Extend `ALL_CHECKS` list in `audit_pg.py` with `"invalid"`, `"unindexed-fks"`, `"dead-tuples"`
- [x] 0.5 Extend `CHECK_FIELDS` dict in `audit_pg.py` with matching entries for the three new checks
- [x] 0.6 Add `pyproject.toml` optional dependency group: `[project.optional-dependencies] mcp = ["asyncpg>=0.29.0", "mcp>=1.0.0,<2", "pydantic>=2.0.0"]`

---

## Phase 1: Query Fixes — 4 SQL Divergences in `audit_pg.py`

> TDD: write the failing test first (RED), then fix the SQL (GREEN).

- [x] 1.1 **RED** — Write test in `tests/test_cli.py` asserting `pg_options_to_table` appears in the fillfactor SQL emitted by the auditor (mock `psycopg2.connect`, capture the query string)
- [x] 1.2 **GREEN** — Replace the `COALESCE(unnest+substr+split_part)` fillfactor subquery in `audit_pg.py` with `SELECT option_value::int FROM pg_options_to_table(c.reloptions) WHERE option_name = 'fillfactor'` (REQ-QC-01)
- [x] 1.3 **RED** — Write test asserting the redundant-index CTE query contains `am.amname = 'btree'` and `i.indisvalid`
- [x] 1.4 **GREEN** — Add `JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am am ON am.oid = c.relam WHERE am.amname = 'btree' AND i.indisvalid` to `parsed_indexes` CTE in `audit_pg.py` (REQ-QC-02)
- [x] 1.5 **RED** — Write test asserting the low-usage query uses threshold `> 1000` (not `> 100`)
- [x] 1.6 **GREEN** — Change `> 100` to `> 1000` in the low-usage writes WHERE clause in `audit_pg.py` (REQ-QC-03)
- [x] 1.7 **RED** — Write/update test in `tests/test_render.py` (`test_render_json_converts_decimal_to_float`) — confirm `hot_ratio_pct` is now a Python `float`, not `Decimal`, when returned from a real query result; update fixture if needed
- [x] 1.8 **GREEN** — Change `ROUND(...)` to `ROUND(...)::float` in the `hot_ratio_pct` expression in `audit_pg.py` (REQ-QC-04); verify `_jsonable()` shim is still present (do NOT remove it)

---

## Phase 2: CLI Critical Checks — 3 New Methods + Exit Code

> TDD: RED → GREEN per check. `has_critical_issues` property was added in Phase 0.

- [x] 2.1 **RED** — Write test for `_audit_invalid_indexes()`: mock `cursor.fetchall` returning one row; assert result is `list[InvalidIndexIssue]` with correct fields
- [x] 2.2 **GREEN** — Implement `_audit_invalid_indexes()` method in `audit_pg.py`: SQL joins `pg_index`/`pg_class`/`pg_stat_user_indexes` where `NOT indisvalid`; returns `list[InvalidIndexIssue]` (REQ-CC-01)
- [x] 2.3 **RED** — Write test for `_audit_unindexed_fks()`: mock cursor; assert result is `list[UnindexedFKIssue]` with correct fields
- [x] 2.4 **GREEN** — Implement `_audit_unindexed_fks()` in `audit_pg.py`: SQL finds FK constraints with no covering index; returns `list[UnindexedFKIssue]` (REQ-CC-02)
- [x] 2.5 **RED** — Write test for `_audit_autovacuum_dead_tuples()`: mock cursor; assert result is `list[DeadTuplesIssue]`; include a row with null `last_autovacuum` to cover the `datetime | None` branch
- [x] 2.6 **GREEN** — Implement `_audit_autovacuum_dead_tuples()` in `audit_pg.py`: SQL queries `pg_stat_user_tables` for dead-tuple ratio; returns `list[DeadTuplesIssue]` (REQ-CC-03)
- [x] 2.7 **RED** — Write test: `DatabaseHealthReport` with non-empty `invalid_indexes` → `has_critical_issues` is `True`; empty report → `False`
- [x] 2.8 **GREEN** — Confirm `has_critical_issues` property (added in 0.3) passes this test (REQ-CC-04); no code change expected if 0.3 was correct
- [x] 2.9 **RED** — Write test for `main()` exit code 3: mock auditor returning report with one `InvalidIndexIssue`; assert `main()` returns `3`
- [x] 2.10 **GREEN** — Update `main()` in `audit_pg.py`: check `report.has_critical_issues` first → `return 3`; then existing `has_issues` check → `return 2`; else `return 0` (REQ-CC-05)

---

## Phase 3: MCP Test Coverage — `tests/test_mcp_auditor.py`

> All cases use `AsyncMock` / `FakeAsyncAuditor` patterns from the design.
> No production code changes in this phase.

- [x] 3.1 Create `tests/test_mcp_auditor.py`; add `FakeAsyncAuditor` helper class that returns a canned `PostgresHealthReport` from `run_full_audit(**kw)`
- [x] 3.2 Write tests MT-01..MT-05: tool-handler happy-path tests via `FakeAsyncAuditor` (one per MCP tool: `pg_health_audit`, `get_invalid_indexes`, `get_unindexed_fks`, `get_dead_tuples`, `get_hot_tables`)
- [x] 3.3 Write tests MT-06..MT-09: `AsyncMock(spec=asyncpg.Connection)` method-level tests — `_audit_invalid_indexes`, `_audit_unindexed_fks`, `_audit_autovacuum_dead_tuples`, `_audit_hot_tables` (assert `conn.fetch` called with expected SQL fragment)
- [x] 3.4 Write tests MT-10..MT-12: error/edge-case tests — empty result sets return empty lists; `asyncpg.PostgresError` propagates as tool error; connection timeout triggers correct exception type
- [x] 3.5 Write tests MT-13..MT-14: `run_full_audit` integration — verify all 6 check methods are called; assert `PostgresHealthReport` fields are populated from mock returns (REQ-MT-01..02)

---

## Phase 4: MCP Config Fixes — `mcp_pg_auditor.py` + `pyproject.toml`

> `pyproject.toml` optional-dep group was added in Phase 0 (task 0.6).

- [x] 4.1 **RED** — Write test: `_run_cli_main()` with a valid `DATABASE_URL` env var resolves without `KeyError`; mock `resolve_db_url` import path to confirm it's imported from `audit_pg` (REQ-PC-02)
- [x] 4.2 **GREEN** — Fix `_run_cli_main()` in `mcp_pg_auditor.py`: import `resolve_db_url` from `audit_pg`; replace current env-var handling with `resolve_db_url()` call
- [x] 4.3 **RED** — Write test: `pg_health_audit()` tool handler accepts `connect_timeout=5`; assert the value is passed through to the connection call
- [x] 4.4 **GREEN** — Add `connect_timeout: int | None = None` parameter to `pg_health_audit()` in `mcp_pg_auditor.py`; thread it through to `asyncpg.connect()` call (REQ-PC-03)
- [x] 4.5 Add `--timeout` argument to `argparse` in `_run_cli_main()` in `mcp_pg_auditor.py`; wire it to `connect_timeout` in the `pg_health_audit()` call (REQ-PC-03)
- [x] 4.6 Add `run_mcp_cli()` sync wrapper function to `mcp_pg_auditor.py` (calls `asyncio.run(mcp_server.run())`); add `[project.scripts] mcp-pg-audit = "mcp_pg_auditor:run_mcp_cli"` entry to `pyproject.toml`

---

## Summary

| Phase | Tasks | Files Touched |
|-------|-------|---------------|
| Phase 0 — Foundation | 6 | `audit_pg.py`, `pyproject.toml` |
| Phase 1 — Query Fixes | 8 | `audit_pg.py`, `tests/test_cli.py`, `tests/test_render.py` |
| Phase 2 — CLI Critical Checks | 10 | `audit_pg.py`, `tests/test_cli.py` |
| Phase 3 — MCP Tests | 5 | `tests/test_mcp_auditor.py` (new) |
| Phase 4 — MCP Config Fixes | 6 | `mcp_pg_auditor.py`, `pyproject.toml` |
| **Total** | **35** | |
