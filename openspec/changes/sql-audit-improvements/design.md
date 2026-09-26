# Design: sql-audit-improvements

## Technical Approach

Fix 4 SQL divergences between CLI and MCP by porting canonical MCP queries into `audit_pg.py`; extend `DatabaseHealthReport` with a computed `has_critical_issues` property and 3 new frozen dataclasses; port 3 MCP-only checks to the CLI; add exit code 3; write `tests/test_mcp_auditor.py` with ≥13 AsyncMock tests; and add optional MCP extras to `pyproject.toml`. The MCP module (`mcp_pg_auditor.py`) is the canonical SQL reference; the CLI syncs to it.

## Architecture Decisions

| # | Decision | Choice | Rejected | Rationale |
|---|----------|--------|----------|-----------|
| D1 | SQL authority | MCP queries are canonical | CLI as reference | MCP uses `pg_options_to_table`, btree+indisvalid guard, threshold 1000 — all demonstrably more correct |
| D2 | `has_critical_issues` placement | `@property` on `DatabaseHealthReport` | Stored field on frozen dataclass | Frozen dataclasses cannot add stored fields without breaking existing keyword-arg constructors; a property is zero-constructor-change backward-compatible |
| D3 | `_jsonable()` fate | Keep as shim; stop relying on it for `hot_ratio_pct` | Remove entirely | After fix 1d, `hot_ratio_pct` is native `float`; `_jsonable` still guards `Decimal` edge cases in other columns — removal is a risky surface change for no gain |
| D4 | Config coupling for `_run_cli_main` | Import `resolve_db_url` directly from `audit_pg` | Extract to shared `pg_config.py` | Both files are flat scripts (not packages); a new module breaks the PEP 723 `# /// script` header and adds a dependency for a single function. Direct import is coherent because `mcp_pg_auditor` is already an explicit companion to `audit_pg` |
| D5 | `_print_cli_summary` testability | Keep print-only; test via `capsys` | Refactor to return string | Function is MCP-internal; `capsys` is the pytest idiom for stdout capture; refactoring changes the public surface unnecessarily |
| D6 | New check dataclasses | Frozen `@dataclass` mirroring MCP Pydantic models | Reuse Pydantic in CLI | CLI is synchronous + psycopg2; no Pydantic dep; frozen dataclasses are zero-cost and match existing conventions |
| D7 | `sql-audit-mcp` scripts entry | Add sync wrapper `run_mcp_cli()` in `mcp_pg_auditor.py` | Point directly at `_run_cli_main` | `[project.scripts]` requires a sync callable; `_run_cli_main` is async — a one-line `asyncio.run()` wrapper is needed |

## Data Flow

### audit_pg.py — extended run_audit() call graph

```
run_audit(checks=[...])
  ├── "invalid"        → _audit_invalid_indexes(cur, schemas)    → list[InvalidIndexIssue]
  ├── "unindexed-fks"  → _audit_unindexed_fks(cur, schemas)      → list[UnindexedFKIssue]
  ├── "dead-tuples"    → _audit_autovacuum_dead_tuples(cur, schemas) → list[DeadTuplesIssue]
  ├── "hot"            → _audit_hot_and_fillfactor(...)           → list[HotUpdateIssue]
  ├── "redundant"      → _audit_redundant_indexes(...)            → list[RedundantIndexIssue]
  └── "low-usage"      → _audit_low_usage_indexes(...)            → list[LowUsageIndexIssue]
  └── DatabaseHealthReport(all 6 lists)
        └── .has_critical_issues (property) → bool(invalid or unindexed_fks or dead_tuples)

main()
  ├── report.has_critical_issues → return 3
  ├── has_issues(report)         → return 2
  ├── error                      → return 1
  └── clean                      → return 0
```

### Exit code ladder

| Code | Meaning | Condition |
|------|---------|-----------|
| 0 | Clean | No issues of any kind |
| 1 | Runtime error | Connection failure, bad args |
| 2 | Warnings | hot/redundant/low-usage issues only |
| 3 | Critical | `has_critical_issues` is True (invalid indexes / unindexed FKs / dead tuples) |

Exit 3 takes precedence over exit 2 (check `has_critical_issues` first in `main()`).

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `audit_pg.py` | Modify | Fix 4 SQL divergences; add 3 dataclasses (`InvalidIndexIssue`, `UnindexedFKIssue`, `DeadTuplesIssue`); add 3 audit methods; extend `DatabaseHealthReport` with `has_critical_issues` property and 3 new fields; extend `ALL_CHECKS` / `CHECK_FIELDS`; update `main()` for exit 3 |
| `mcp_pg_auditor.py` | Modify | Import `resolve_db_url` from `audit_pg` in `_run_cli_main()`; add `connect_timeout` param to `pg_health_audit()` tool; add `run_mcp_cli()` sync wrapper |
| `tests/test_mcp_auditor.py` | Create | ≥13 AsyncMock-based tests for `AsyncPostgresHealthAuditor` and `pg_health_audit` tool handler |
| `pyproject.toml` | Modify | Add `[project.optional-dependencies] mcp = [...]`; add `sql-audit-mcp` script entry |

## Interfaces / Contracts

### New dataclasses in audit_pg.py

```python
@dataclass(frozen=True)
class InvalidIndexIssue:
    child_table: str
    invalid_index: str
    index_size: str


@dataclass(frozen=True)
class UnindexedFKIssue:
    child_table: str
    fk_name: str
    parent_table: str
    fk_definition: str


@dataclass(frozen=True)
class DeadTuplesIssue:
    table_name: str
    dead_tuples: int
    live_tuples: int
    dead_tuple_pct: float
    last_autovacuum: datetime | None = None
    last_vacuum: datetime | None = None
```

### DatabaseHealthReport — backward-compatible extension

```python
@dataclass(frozen=True)
class DatabaseHealthReport:
    # existing fields — unchanged, no constructor breakage
    hot_issues: list[HotUpdateIssue] = field(default_factory=list)
    redundant_indexes: list[RedundantIndexIssue] = field(default_factory=list)
    low_usage_indexes: list[LowUsageIndexIssue] = field(default_factory=list)
    # new fields — all defaulting to [] so existing tests pass unchanged
    invalid_indexes: list[InvalidIndexIssue] = field(default_factory=list)
    unindexed_fks: list[UnindexedFKIssue] = field(default_factory=list)
    autovacuum_dead_tuples: list[DeadTuplesIssue] = field(default_factory=list)

    @property
    def has_critical_issues(self) -> bool:
        return bool(self.invalid_indexes or self.unindexed_fks or self.autovacuum_dead_tuples)
```

All 28 existing tests construct `DatabaseHealthReport()` using keyword args for the original 3 fields. Adding new keyword-defaulted fields does NOT break them.

### Extended ALL_CHECKS and CHECK_FIELDS

```python
ALL_CHECKS = ["redundant", "hot", "low-usage", "invalid", "unindexed-fks", "dead-tuples"]

CHECK_FIELDS: dict[str, str] = {
    "redundant": "redundant_indexes",
    "hot": "hot_issues",
    "low-usage": "low_usage_indexes",
    "invalid": "invalid_indexes",
    "unindexed-fks": "unindexed_fks",
    "dead-tuples": "autovacuum_dead_tuples",
}
```

### New audit method signatures (psycopg2 `%s` placeholders)

```python
def _audit_invalid_indexes(
    self, cur: Any, schemas: list[str] | None
) -> list[InvalidIndexIssue]: ...


def _audit_unindexed_fks(self, cur: Any, schemas: list[str] | None) -> list[UnindexedFKIssue]: ...


def _audit_autovacuum_dead_tuples(
    self, cur: Any, schemas: list[str] | None
) -> list[DeadTuplesIssue]: ...
```

SQL is ported directly from `mcp_pg_auditor.py` with `$N` placeholders replaced by `%s`.

### SQL divergence fixes (exact changes to audit_pg.py)

**1a — fillfactor parsing** (inside `_audit_hot_and_fillfactor` COALESCE subquery):

```sql
-- REMOVE:
SELECT split_part(opt, '=', 2)::int
FROM unnest(c.reloptions) AS opt
WHERE substr(opt, 1, 11) = 'fillfactor='
LIMIT 1

-- REPLACE WITH:
SELECT option_value::int
FROM pg_options_to_table(c.reloptions)
WHERE option_name = 'fillfactor'
```

**1b — redundant indexes CTE** (add to `parsed_indexes` inner join chain):

```sql
-- ADD after FROM pg_index i:
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_am am ON am.oid = c.relam
-- ADD to WHERE:
WHERE am.amname = 'btree' AND i.indisvalid
```

**1c — low-usage table_writes threshold**:

```sql
-- CHANGE:
WHERE (t.n_tup_ins + t.n_tup_upd + t.n_tup_del) > 100
-- TO:
WHERE (t.n_tup_ins + t.n_tup_upd + t.n_tup_del) > 1000
```

**1d — hot_ratio_pct explicit float cast**:

```sql
-- CHANGE:
ROUND(...)::numeric AS hot_ratio_pct
-- TO:
ROUND(...)::float AS hot_ratio_pct
```

After 1d, `HotUpdateIssue.hot_ratio_pct` is native `float`; `_jsonable()` retained as a shim but no longer critical for this field.

### pyproject.toml additions

```toml
[project.optional-dependencies]
mcp = [
    "asyncpg>=0.29.0",
    "fastmcp>=2.0.0",
    "pydantic>=2.0.0",
]

[project.scripts]
sql-audit     = "audit_pg:main"
sql-audit-mcp = "mcp_pg_auditor:run_mcp_cli"
```

### mcp_pg_auditor.py — _run_cli_main config fix

```python
# Replace DSN resolution block (lines ~458-461):
from audit_pg import resolve_db_url  # add import at function top

dsn = resolve_db_url(cli_url=args.dsn)  # 5-step chain from audit_pg
if not dsn:
    dsn = resolve_dsn(args.alias)  # fallback to alias-based env var
```

Also add `--timeout` flag to `_run_cli_main` argparse and pass it to `AsyncPostgresHealthAuditor`.

### pg_health_audit tool — connect_timeout addition

```python
async def pg_health_audit(
    schemas: list[str] = ["public"],
    enabled_checks: list[CheckName] | None = None,
    min_table_rows: int = 1000,
    min_size_bytes: int = 10_000_000,
    db_alias: str = "default",
    connect_timeout: int | None = None,  # NEW — None = use auditor default (10s)
) -> PostgresHealthReport:
    ...
    executor = AsyncPostgresHealthAuditor(
        dsn=dsn,
        connect_timeout=connect_timeout if connect_timeout is not None else 10,
    )
```

### Sync wrapper for scripts entry point

```python
def run_mcp_cli() -> None:
    """Sync entry point for [project.scripts] — asyncio wrapper for _run_cli_main."""
    import asyncio

    asyncio.run(_run_cli_main())
```

## Testing Strategy

| Layer | What to Test | Approach |
|-------|-------------|----------|
| Unit — new dataclasses | Construction, immutability | Direct instantiation; `pytest.raises(FrozenInstanceError)` |
| Unit — `has_critical_issues` | Truth table: all empty, each field populated, mixed | `DatabaseHealthReport` factory; assert property value |
| Unit — exit code 3 | `main()` returns 3 when `has_critical_issues=True` | `FakeAuditor` with non-empty `invalid_indexes` |
| Unit — exit 2 unchanged | Hot/redundant/low-usage still → 2 | Existing tests — no change needed |
| Unit — MCP `_audit_invalid_indexes` | Returns `list[InvalidIndexIssue]` from mock rows | `AsyncMock` on `conn.fetch` returning raw dicts |
| Unit — MCP `_audit_unindexed_fks` | Returns `list[UnindexedFKIssue]` from mock rows | Same pattern |
| Unit — MCP `_audit_autovacuum_dead_tuples` | Returns `list[DeadTuplesIssue]` from mock rows | Same pattern |
| Unit — MCP `run_full_audit` | `has_critical_issues` set when critical checks non-empty | Patched per-method, assert report field |
| Unit — `pg_health_audit` tool | Calls auditor, sets `execution_time_ms > 0` | `FakeAsyncAuditor` via monkeypatch |
| Unit — `pg_health_audit` connect_timeout | Passes through to auditor constructor | Assert constructor call args |
| Unit — `_print_cli_summary` | Output sections present per check category | `capsys.readouterr().out` |
| Unit — `_run_cli_main` config | Uses `resolve_db_url` 5-step chain | Monkeypatch env vars |
| Unit — `_run_cli_main` timeout flag | `--timeout` parsed and passed to auditor | Assert constructor call args |

### FakeAsyncAuditor pattern (mirrors FakeAuditor in test_cli.py)

```python
def make_async_auditor_factory(report: PostgresHealthReport):
    class FakeAsyncAuditor:
        async def run_full_audit(self, **kw) -> PostgresHealthReport:
            return report

    return FakeAsyncAuditor()
```

`AsyncMock` on `conn.fetch` is used for method-level unit tests; `FakeAsyncAuditor` is used for tool-handler integration tests.

## Migration / Rollout

No migration required. All changes are backward-compatible:

- Existing 28 tests pass unchanged — new `DatabaseHealthReport` fields have defaults; no constructor signatures broken.
- Exit code 2 semantics preserved for all existing consumers; exit 3 is strictly additive.
- Optional MCP extras do not affect base `pip install herraminetas-sql`.
- `render_text()`, `render_json()`, and `render_quiet()` do **not** need updates in this change (they only render existing check categories). Extending them to display the 3 new check categories is out of scope and tracked as a follow-up.

## Open Questions

- [ ] `render_text()` / `render_json()` / `render_quiet()` do not display the 3 new checks — intentionally deferred. Should be a follow-up task.
- [ ] `sql-audit-mcp` script entry requires `run_mcp_cli()` sync wrapper to be added to `mcp_pg_auditor.py` before `pyproject.toml` entry is valid — tasks must order this correctly.
