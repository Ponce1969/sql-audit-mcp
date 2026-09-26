# Spec: sql-audit-improvements
<!-- topic_key: sdd/sql-audit-improvements/spec -->
<!-- project: Herraminetas_Sql -->
<!-- type: architecture -->

## Overview

This spec covers 4 capability areas for the `sql-audit-improvements` change:

1. **query-correctness** — Fix 4 silent divergences between `audit_pg.py` (CLI) and `mcp_pg_auditor.py` (MCP)
2. **cli-critical-checks** — Port 3 checks from MCP to CLI + add `has_critical_issues` + exit code 3
3. **mcp-test-coverage** — Write `tests/test_mcp_auditor.py` with ≥13 test cases
4. **packaging-config** — Add MCP optional deps, fix `_run_cli_main()` DSN reuse, expose `connect_timeout`

---

## 1. query-correctness

### REQ-QC-01 — Fillfactor Parsing via `pg_options_to_table()`

The CLI `_audit_hot_and_fillfactor()` MUST replace the current `split_part(opt,'=',2)::int` + `substr(opt,1,11)='fillfactor='` pattern with `pg_options_to_table(c.reloptions)` to match the MCP implementation.

**Rationale:** `substr()` + `split_part()` is fragile — any future option with a prefix matching `fillfactor` can corrupt the parse. `pg_options_to_table()` is the idiomatic, type-safe approach.

#### Scenario QC-01a — Table with explicit fillfactor
```
Given a table `orders` with `reloptions = '{fillfactor=70}'`
When the CLI runs the `hot` check
Then the returned `HotUpdateIssue.fillfactor` for `orders` MUST be 70
And `fillfactor_warning` MUST be False
```

#### Scenario QC-01b — Table with no fillfactor option
```
Given a table `users` with `reloptions = NULL` (or empty array)
When the CLI runs the `hot` check
Then the returned `HotUpdateIssue.fillfactor` for `users` MUST be 100 (COALESCE default)
And `fillfactor_warning` MUST be True
```

#### Scenario QC-01c — Result parity with MCP
```
Given identical row data returned from the database
When both CLI and MCP run the `hot` check against the same table
Then both MUST return the same `fillfactor` integer value for every row
```

---

### REQ-QC-02 — Redundant Indexes: btree-only + `indisvalid` Guard

The CLI `_audit_redundant_indexes()` MUST add:
- A `JOIN pg_am am ON am.oid = c.relam WHERE am.amname = 'btree'` filter in the CTE
- An `AND i.indisvalid` guard in the CTE

**Rationale:** GiST, GIN, BRIN, and Hash indexes have different containment semantics; the key-prefix logic is btree-specific and produces false positives on other index types. Invalid indexes cannot cover any query and must not be reported as "covering" a redundant one.

#### Scenario QC-02a — GIN index excluded from redundant analysis
```
Given a table `posts` with a GIN index `idx_posts_gin` on column `body`
And a btree index `idx_posts_body` on column `body` with identical key ordering
When the CLI runs the `redundant` check
Then `idx_posts_gin` MUST NOT appear as the `covering_index` in any result row
And `idx_posts_gin` MUST NOT appear as the `redundant_index` in any result row
```

#### Scenario QC-02b — Invalid index excluded
```
Given a btree index `idx_orders_status` with `indisvalid = FALSE`
And another valid btree index `idx_orders_status_v2` with keys that are a superset
When the CLI runs the `redundant` check
Then `idx_orders_status` MUST NOT appear in any result row as either redundant or covering
```

#### Scenario QC-02c — Valid btree duplicate still detected
```
Given two valid btree indexes on `orders` where `idx_orders_a` is a left-prefix of `idx_orders_b`
When the CLI runs the `redundant` check
Then `idx_orders_a` MUST appear as `redundant_index` covered by `idx_orders_b`
```

---

### REQ-QC-03 — Low-Usage Threshold Alignment to `> 1000`

The CLI `_audit_low_usage_indexes()` MUST change the `WHERE` clause threshold from `> 100` to `> 1000` total writes.

**Rationale:** Tables with fewer than 1000 writes have insufficient signal for ratio-based analysis; the MCP uses `> 1000` as the minimum meaningful sample size.

#### Scenario QC-03a — Table with exactly 1000 writes excluded
```
Given a table `sessions` where `n_tup_ins + n_tup_upd + n_tup_del = 1000`
And an index `idx_sessions_token` with `idx_scan = 0`
When the CLI runs the `low-usage` check
Then `idx_sessions_token` MUST NOT appear in the results
```

#### Scenario QC-03b — Table with 1001 writes included
```
Given a table `events` where `n_tup_ins + n_tup_upd + n_tup_del = 1001`
And an index `idx_events_type` with `idx_scan = 0` and sufficient size
When the CLI runs the `low-usage` check
Then `idx_events_type` MUST appear in the results
```

#### Scenario QC-03c — Threshold parity with MCP
```
Given identical catalog data
When both CLI and MCP run the `low-usage` / `low_usage_indexes` check
Then both MUST return the same set of flagged indexes (no row present in one but absent in the other due to threshold difference)
```

---

### REQ-QC-04 — `hot_ratio_pct` Float Cast in CLI

The CLI `_audit_hot_and_fillfactor()` SQL MUST add `::float` after the `ROUND(...)` expression for `hot_ratio_pct`.

**Rationale:** Without the explicit cast, psycopg2 returns a Python `Decimal` for `ROUND()`. The MCP uses asyncpg which natively returns `float`. This divergence causes the `_jsonable()` workaround and makes JSON outputs between the two inconsistent.

#### Scenario QC-04a — `hot_ratio_pct` is a Python float
```
Given a table `orders` with `n_tup_upd = 1000` and `n_tup_hot_upd = 500`
When the CLI runs the `hot` check
Then `HotUpdateIssue.hot_ratio_pct` MUST be of Python type `float`
And its value MUST equal `50.0` (not `Decimal('50.00')`)
```

#### Scenario QC-04b — JSON output contains a number, not a string
```
Given the same table conditions as QC-04a
When the CLI runs with `--json`
Then the JSON `hot_ratio_pct` field MUST serialize as a JSON number (not a quoted string)
```

#### Scenario QC-04c — `_jsonable()` no longer needed for `hot_ratio_pct`
```
Given a `DatabaseHealthReport` with hot issues whose `hot_ratio_pct` is a native float
When `render_json()` is called
Then `_jsonable()` MUST NOT be required to convert `hot_ratio_pct` (it passes through as-is)
```

---

## 2. cli-critical-checks

### REQ-CC-01 — Port `invalid_indexes` Check to CLI

The CLI `audit_pg.py` MUST implement an `_audit_invalid_indexes()` method on `PostgresHealthAuditor` using the same SQL logic as the MCP. `"invalid_indexes"` MUST be added to `ALL_CHECKS`. `DatabaseHealthReport` MUST gain an `invalid_indexes: list[InvalidIndexIssue]` field.

#### Scenario CC-01a — Invalid index detected
```
Given a PostgreSQL index with `indisvalid = FALSE` in schema `public`
When the CLI runs the `invalid_indexes` check
Then an `InvalidIndexIssue` entry MUST be returned with correct `child_table`, `invalid_index`, and `index_size`
```

#### Scenario CC-01b — No invalid indexes returns empty list
```
Given all indexes in schema `public` have `indisvalid = TRUE`
When the CLI runs the `invalid_indexes` check
Then `DatabaseHealthReport.invalid_indexes` MUST be an empty list
```

#### Scenario CC-01c — Schema filter applies
```
Given an invalid index in schema `analytics` and no invalid indexes in schema `public`
When the CLI runs the `invalid_indexes` check with `--schema public`
Then `DatabaseHealthReport.invalid_indexes` MUST be an empty list
```

---

### REQ-CC-02 — Port `unindexed_fks` Check to CLI

The CLI MUST implement `_audit_unindexed_fks()` on `PostgresHealthAuditor` using the same SQL as the MCP. `"unindexed_fks"` MUST be added to `ALL_CHECKS`. `DatabaseHealthReport` MUST gain an `unindexed_fks: list[UnindexedFKIssue]` field.

#### Scenario CC-02a — Unindexed FK detected
```
Given table `order_items` with a FK to `orders` where no index covers the FK columns
When the CLI runs the `unindexed_fks` check
Then an `UnindexedFKIssue` MUST be returned with `child_table='order_items'`, `parent_table='orders'`, non-empty `fk_name`, and non-empty `fk_definition`
```

#### Scenario CC-02b — Indexed FK not reported
```
Given table `order_items` where the FK columns ARE covered by a valid btree index
When the CLI runs the `unindexed_fks` check
Then no `UnindexedFKIssue` MUST be returned for `order_items`
```

---

### REQ-CC-03 — Port `autovacuum_dead_tuples` Check to CLI

The CLI MUST implement `_audit_autovacuum_dead_tuples()` on `PostgresHealthAuditor` using the same SQL as the MCP. `"autovacuum_dead_tuples"` MUST be added to `ALL_CHECKS`. `DatabaseHealthReport` MUST gain an `autovacuum_dead_tuples: list[DeadTuplesIssue]` field.

#### Scenario CC-03a — Table with high dead tuple percentage detected
```
Given table `logs` with `n_dead_tup = 50000`, `n_live_tup = 100000` (dead_pct ≈ 33%)
When the CLI runs the `autovacuum_dead_tuples` check
Then a `DeadTuplesIssue` MUST be returned with `table_name='logs'`, `dead_tuples=50000`, and `dead_tuple_pct > 15.0`
```

#### Scenario CC-03b — Table below threshold not reported
```
Given table `sessions` with `n_dead_tup = 5000` and `n_live_tup = 1000000` (dead_pct < 1%)
When the CLI runs the `autovacuum_dead_tuples` check
Then `sessions` MUST NOT appear in the results
```

---

### REQ-CC-04 — `has_critical_issues` Field in `DatabaseHealthReport`

`DatabaseHealthReport` MUST gain a `has_critical_issues: bool` field. It MUST be computed as `True` when any of `invalid_indexes`, `unindexed_fks`, or `autovacuum_dead_tuples` is non-empty.

#### Scenario CC-04a — `has_critical_issues` is True when invalid index exists
```
Given a `DatabaseHealthReport` with `invalid_indexes` containing one entry
When `has_critical_issues` is evaluated
Then it MUST be True
```

#### Scenario CC-04b — `has_critical_issues` is False when only non-critical checks have findings
```
Given a `DatabaseHealthReport` with `redundant_indexes` containing entries but `invalid_indexes`, `unindexed_fks`, and `autovacuum_dead_tuples` all empty
When `has_critical_issues` is evaluated
Then it MUST be False
```

#### Scenario CC-04c — `has_critical_issues` False when all lists are empty
```
Given a completely clean `DatabaseHealthReport` (all lists empty)
When `has_critical_issues` is evaluated
Then it MUST be False
```

---

### REQ-CC-05 — Exit Code 3 for Critical Issues

The CLI `main()` MUST return exit code `3` when `report.has_critical_issues` is True. The existing exit code `2` (non-critical issues) and `0` (clean) MUST remain unchanged.

**Breaking-change notice:** Any CI script that checks `exit == 2` to detect "any issues" MUST be updated to handle `exit in (2, 3)`. This MUST be documented in CHANGELOG and README.

#### Scenario CC-05a — Exit 3 when critical issue present
```
Given a `DatabaseHealthReport` with `has_critical_issues = True`
When `main()` runs
Then the return value MUST be 3
```

#### Scenario CC-05b — Exit 2 when only non-critical issues
```
Given a `DatabaseHealthReport` with `redundant_indexes` non-empty but `has_critical_issues = False`
When `main()` runs
Then the return value MUST be 2
```

#### Scenario CC-05c — Exit 0 when all checks pass
```
Given a clean `DatabaseHealthReport` with all lists empty
When `main()` runs
Then the return value MUST be 0
```

#### Scenario CC-05d — Exit 1 on connection error (unchanged)
```
Given the auditor raises `DatabaseConnectionError`
When `main()` runs
Then the return value MUST be 1
```

---

## 3. mcp-test-coverage

### REQ-MT-01 — Test File `tests/test_mcp_auditor.py` MUST be Created

The file MUST use `pytest`, `pytest-asyncio`, and `unittest.mock.AsyncMock` to mock `asyncpg.Connection`. Tests MUST follow the existing `test_cli.py` fake-injection pattern adapted for async.

### REQ-MT-02 — Test Suite MUST Cover ≥13 Distinct Behaviors

The test suite MUST include at minimum the following behaviors:

#### Scenario MT-01 — `resolve_dsn` resolves `DATABASE_URL` for default alias
```
Given `DATABASE_URL=postgresql://u:p@localhost/db` in the environment
When `resolve_dsn("default")` is called
Then it MUST return `"postgresql://u:p@localhost/db"`
```

#### Scenario MT-02 — `resolve_dsn` resolves `DB_{ALIAS}_URL` for named alias
```
Given `DB_PROD_URL=postgresql://u:p@prod/mydb` in the environment
When `resolve_dsn("prod")` is called
Then it MUST return `"postgresql://u:p@prod/mydb"`
```

#### Scenario MT-03 — `resolve_dsn` raises `ValueError` when env var absent
```
Given neither `DATABASE_URL` nor `DB_STAGING_URL` is set
When `resolve_dsn("staging")` is called
Then a `ValueError` MUST be raised containing the alias name `"staging"`
```

#### Scenario MT-04 — `_audit_invalid_indexes` returns mapped DTOs
```
Given a mocked `asyncpg.Connection.fetch()` returning one row `{child_table="orders", invalid_index="idx_orders_status", index_size="16 kB"}`
When `AsyncPostgresHealthAuditor._audit_invalid_indexes(conn, ["public"])` is awaited
Then the result MUST contain exactly one `InvalidIndexIssue` with matching field values
```

#### Scenario MT-05 — `_audit_invalid_indexes` returns empty list when no rows
```
Given `conn.fetch()` returns an empty list
When `_audit_invalid_indexes` is awaited
Then the result MUST be an empty list
```

#### Scenario MT-06 — `_audit_unindexed_fks` maps FK rows correctly
```
Given a mocked `conn.fetch()` returning one FK row with all required fields
When `_audit_unindexed_fks` is awaited
Then the result MUST contain one `UnindexedFKIssue` with correct field values
```

#### Scenario MT-07 — `_audit_autovacuum_dead_tuples` maps dead tuple rows correctly
```
Given a mocked `conn.fetch()` returning one row with `last_autovacuum=None`
When `_audit_autovacuum_dead_tuples` is awaited
Then the result MUST contain one `DeadTuplesIssue` with `last_autovacuum=None`
```

#### Scenario MT-08 — `run_full_audit` with all checks enabled returns complete report
```
Given all six `conn.fetch()` calls return one row each
When `run_full_audit(schemas=["public"], checks=[all six check names])` is awaited
Then the returned `PostgresHealthReport` MUST have each list with exactly one entry
And `has_critical_issues` MUST be True (invalid_indexes is non-empty)
```

#### Scenario MT-09 — `run_full_audit` with partial check selection skips omitted checks
```
Given only `invalid_indexes` is in the checks list
When `run_full_audit` is awaited
Then `conn.fetch()` MUST be called exactly once (only the invalid_indexes query)
And all other lists in the report MUST be empty
```

#### Scenario MT-10 — `has_critical_issues` is True only when critical checks have findings
```
Given `_audit_invalid_indexes` returns one entry, all others return empty
When `run_full_audit` is awaited
Then `PostgresHealthReport.has_critical_issues` MUST be True
```

#### Scenario MT-11 — `has_critical_issues` is False when only non-critical checks have findings
```
Given `_audit_redundant_indexes` returns entries, but `invalid_indexes`, `unindexed_fks`, `autovacuum_dead_tuples` all return empty
When `run_full_audit` is awaited
Then `PostgresHealthReport.has_critical_issues` MUST be False
```

#### Scenario MT-12 — `pg_health_audit` MCP tool propagates `connect_timeout`
```
Given `DATABASE_URL` is set
And `pg_health_audit` is called with `connect_timeout=5`
When `asyncpg.connect()` is called internally
Then it MUST be called with `timeout=5`
```

#### Scenario MT-13 — `pg_health_audit` raises on missing DSN
```
Given no `DATABASE_URL` env var and no matching `DB_{ALIAS}_URL`
When `pg_health_audit(db_alias="nonexistent")` is awaited
Then a `ValueError` MUST be raised before any `asyncpg.connect()` is attempted
```

#### Scenario MT-14 — Schema filter list is forwarded to all check methods
```
Given `run_full_audit` is called with `schemas=["analytics", "reporting"]`
When the async auditor runs all checks
Then every `conn.fetch()` call MUST receive `["analytics", "reporting"]` as the schema parameter
```

---

## 4. packaging-config

### REQ-PC-01 — Optional MCP Dependencies in `pyproject.toml`

`pyproject.toml` MUST declare an `[project.optional-dependencies]` group named `mcp` with pinned lower bounds:

```toml
[project.optional-dependencies]
mcp = [
    "asyncpg>=0.29.0",
    "mcp>=1.0.0,<2",
    "pydantic>=2.0.0",
]
```

The core `[project.dependencies]` list MUST NOT change (psycopg2-binary and python-dotenv remain the only hard deps).

#### Scenario PC-01a — Install with extras succeeds
```
Given a fresh virtual environment
When `pip install herraminetas-sql[mcp]` is run
Then `asyncpg`, `mcp`, and `pydantic` packages MUST be installed
```

#### Scenario PC-01b — Install without extras does not pull MCP deps
```
Given a fresh virtual environment
When `pip install herraminetas-sql` is run (no extras)
Then `asyncpg` and `mcp` MUST NOT be installed
```

---

### REQ-PC-02 — `_run_cli_main()` MUST Reuse `resolve_db_url()` from `audit_pg`

The current `_run_cli_main()` in `mcp_pg_auditor.py` does its own `os.getenv("DATABASE_URL")` lookup and falls back to `resolve_dsn()`. It MUST instead import and call `resolve_db_url()` from `audit_pg` for the full resolution chain (explicit URL > env-file > dotenv > PG* vars).

#### Scenario PC-02a — `--dsn` flag takes precedence
```
Given `_run_cli_main()` is invoked with `--dsn postgresql://u:p@host/db`
When the DSN is resolved
Then the provided `--dsn` value MUST be used directly, without reading any env var
```

#### Scenario PC-02b — Falls back to full env resolution chain when no `--dsn`
```
Given no `--dsn` flag is provided
And `DATABASE_URL` is not set
And `PGHOST=pg.internal`, `PGDATABASE=mydb`, `PGUSER=app` are set
When `_run_cli_main()` resolves the DSN
Then a valid libpq-style URL using those PG* vars MUST be used
```

#### Scenario PC-02c — Exits with error when no config found
```
Given no `--dsn`, no `DATABASE_URL`, no PG* vars, and no `.env` file
When `_run_cli_main()` is invoked
Then it MUST exit with a non-zero code and print a human-readable error to stderr
```

---

### REQ-PC-03 — `connect_timeout` Exposed in MCP Tool Parameter

The `pg_health_audit` MCP tool function MUST accept a `connect_timeout: int` parameter (default: 10). It MUST be forwarded to `AsyncPostgresHealthAuditor.__init__(connect_timeout=...)`.

#### Scenario PC-03a — Custom timeout is forwarded to the auditor
```
Given `pg_health_audit` is called with `connect_timeout=30`
When `AsyncPostgresHealthAuditor` is instantiated internally
Then `connect_timeout` MUST be set to 30
```

#### Scenario PC-03b — Default timeout of 10 applies when not specified
```
Given `pg_health_audit` is called without `connect_timeout`
When `AsyncPostgresHealthAuditor` is instantiated internally
Then `connect_timeout` MUST be set to 10
```

#### Scenario PC-03c — MCP CLI mode exposes `--timeout` flag
```
Given `_run_cli_main()` is invoked with `--timeout 15`
When `AsyncPostgresHealthAuditor` is instantiated
Then `connect_timeout` MUST be set to 15
```

---

## Edge Cases and Constraints

### EC-01 — Partial check selection in CLI
```
Given `--checks hot,invalid_indexes` is passed
When the CLI runs
Then ONLY `_audit_hot_and_fillfactor` and `_audit_invalid_indexes` MUST be called
And the report MUST contain empty lists for all other checks
```

### EC-02 — Schema filtering applies to all new checks
All three new CLI checks (`invalid_indexes`, `unindexed_fks`, `autovacuum_dead_tuples`) MUST honor `--schema` filtering using `= ANY(%s)` parameterized SQL.

### EC-03 — Empty database (all checks return nothing)
```
Given a freshly created schema with no user tables or indexes
When ALL checks are run
Then DatabaseHealthReport MUST have all lists empty, `has_critical_issues = False`, and exit code MUST be 0
```

### EC-04 — Connection timeout honored for new checks
All three new checks run within the same connection opened in `run_audit()`. The `connect_timeout` passed at construction time MUST be used.

### EC-05 — `_jsonable()` backward compatibility
The `_jsonable()` helper in `audit_pg.py` MUST NOT be deleted without first verifying (via grep) there are no callers beyond `render_json()`. If the only usage is the now-unnecessary `Decimal` conversion for `hot_ratio_pct`, it MAY be removed but only after that verification.

---

## Acceptance Summary

| Area | Requirements | Scenarios |
|------|-------------|-----------|
| query-correctness | 4 (QC-01–04) | 12 |
| cli-critical-checks | 5 (CC-01–05) | 12 |
| mcp-test-coverage | 2 (MT-01–02, 14 behaviors) | 14 |
| packaging-config | 3 (PC-01–03) | 9 |
| **Total** | **14** | **47** |
