# PostgreSQL Health Auditor (`sql-audit-mcp`)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![PostgreSQL](https://img.shields.io/badge/postgresql-14%20|%2015%20|%2016-336791.svg)](https://www.postgresql.org/)
[![FastMCP](https://img.shields.io/badge/MCP-FastMCP%20Server-green.svg)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Powered by uv](https://img.shields.io/badge/packaging-uv-DE5FE9.svg)](https://docs.astral.sh/uv/)
[![Tests Passing](https://img.shields.io/badge/tests-143%20passed-brightgreen.svg)](tests/)

A deterministic, zero-hallucination PostgreSQL health and performance auditor designed for production workloads. Runs both as a high-performance standalone CLI and as an on-demand [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for AI coding assistants (Antigravity, Cursor, Claude Desktop, Pi).

Catches silent performance bottlenecks, table-level locking hazards, dead tuple accumulation, broken HOT updates, physical disk bloat, and query execution bottlenecks via direct, non-invasive catalog inspection with zero LLM guesswork.

---

## Architectural Philosophy

- **Deterministic Inspection Over Stochastic Triage**: System catalogs (`pg_catalog`, `pg_stat_*`) provide authoritative database-state evidence. No LLM should "guess" whether an index is missing or redundant when PostgreSQL catalogs provide the exact answer in microseconds with zero VRAM overhead.
- **Canonical Domain Contract**: Standardized `AuditReport`, `Finding`, and `Evidence` schema with deterministic SHA-256 digests. Decouples identity (`finding_id`) from transient observation metrics (`evidence_id`), enabling reliable state tracking across time.
- **Differential Auditing (Drift Engine)**: Compares current inspection against historical baselines, surfacing `new`, `resolved`, `changed`, and `unchanged` findings to eliminate alert fatigue.
- **Operational Risk Analysis**: Explains architectural principles, table-locking risks, and diagnostic context without prescribing unsafe or automated remediation commands.
- **Zero-Trust Credential Security**: Credentials and connection strings are resolved strictly on the host/server side (`DATABASE_URL` or `.env`). Passwords never travel over JSON-RPC protocols or touch LLM context windows.

---

## Core Capabilities

### 1. Storage & Index Health Checks (6 Critical Anti-patterns)
| Check | Target Catalog / Metric | Operational Impact |
|---|---|---|
| **Invalid Indexes** | `pg_index.indisvalid = false` | Aborted concurrent index builds leave dead structures that penalize every `INSERT`/`UPDATE` while planner ignores them. |
| **Unindexed Foreign Keys** | `pg_constraint` vs `pg_index` left-prefix slice | Missing B-Tree index on referencing column acquires heavy `SHARE ROW EXCLUSIVE` table-level locks during parent `UPDATE`/`DELETE`. |
| **Autovacuum & Dead Tuples Lag** | `pg_stat_user_tables.n_dead_tup` | Identifies tables with `> 10,000` dead tuples and `> 15%` dead tuple pressure where autovacuum is blocked or lagging. |
| **Broken HOT Updates** | `n_tup_hot_upd / n_tup_upd < 30%` & `fillfactor` | High-update tables failing Heap-Only Tuple optimization. Alerts when default `fillfactor = 100` prevents in-page updates and generates excessive WAL traffic. |
| **Redundant B-Tree Indexes** | CTE prefix matching `p2.keys[1:len(p1)] = p1.keys` | Detects duplicate indexes and prefix-redundant indexes strictly for B-Tree (`amname = 'btree'`), saving disk, memory cache, and write I/O. |
| **Low-Usage / Unprofitable Indexes** | `idx_scan / (writes) < 0.05` | Indexes with high maintenance write overhead and negligible read lookups on tables with `> 1,000` writes. |

### 2. Physical Disk Bloat Estimation (`--bloat` / `pg_bloat`)
Calculates expected vs. actual disk pages for tables and B-tree indexes based on column alignment, padding, and null bitmaps **without requiring the heavy `pgstattuple` extension**. Provides statistical bloat estimations (estimated bloat ratio and bytes wasted) derived from catalog metadata.

### 3. Real-Time Locks & Blocking Tree (`--locks` / `pg_locks`)
Inspects active lock contention on-demand using `pg_blocking_pids()` and `pg_stat_activity`. Reconstructs the observed blocking dependency graph, isolates root blocker PIDs, and renders an ASCII blocking tree with wait events and process states.

### 4. Query Plan Simulation & Bottleneck Detection (`--explain` / `pg_explain`)
Parses PostgreSQL JSON execution plans via strictly passive `EXPLAIN (FORMAT JSON, COSTS)` to detect sequential scans on large tables, nested loops with high row counts, sort/hash spills to disk (`work_mem` exhaustion), and plan cardinality misestimates. Does not execute audited queries.

### 5. Differential Baseline Audit (`--diff <baseline.json>`)
Compares an existing audit baseline against the live database, isolating newly introduced regressions (+), resolved issues (-), and fluctuating metrics (~).

---

## Installation & Packaging

### Standalone CLI (via `uv`)
```bash
# Global tool installation
uv tool install .

# Or run ad-hoc with PEP 723 metadata (zero manual setup)
uv run audit_pg.py --url "postgresql://user:pass@localhost:5432/dbname"
```

### With MCP Server Dependencies
```bash
uv pip install -e ".[mcp]"
```

---

## CLI Usage Guide

The synchronous CLI is powered by `psycopg2-binary` and provides rich formatting and return codes:

```bash
# 1. Full health audit (auto-discovers .env)
sql-audit

# 2. Explicit connection string
sql-audit --url "postgresql://user:password@localhost:5432/dbname"

# 3. Filter by schemas and table thresholds (defaults: min-table-rows=10000, min-size=0)
sql-audit --schema public,analytics --min-table-rows 10000 --min-size 10000000

# 4. Canonical JSON output (structured AuditReport contract)
sql-audit --canonical-json > baseline.json

# 5. Differential audit against baseline snapshot
sql-audit --diff baseline.json

# 6. Real-time lock contention & blocking tree
sql-audit --locks

# 7. Physical disk bloat estimation
sql-audit --bloat --min-bloat-bytes 10000000 --min-bloat-ratio 20.0

# 8. Query plan simulation (syntax / cost analysis via passive EXPLAIN)
sql-audit --explain "SELECT * FROM orders WHERE total > 100"

# 9. Query plan from file (strictly passive EXPLAIN)
sql-audit --explain-file query.sql

# 10. Quiet mode (summary counts only)
sql-audit --quiet
```

### Canonical Thresholds & Contract
Both CLI and MCP adapters adhere to the unified canonical contract:
- `min_table_rows = 10,000`: Tables with fewer live rows are skipped for HOT update efficiency and low-usage checks to eliminate false positives on micro-tables.
- `min_size_bytes = 0`: All indexes matching redundancy patterns are evaluated regardless of on-disk size.

### Defensive Session Timeouts
To guarantee that passive inspection never hangs on tables undergoing concurrent maintenance:
- `statement_timeout = 15,000 ms` (15s): Enforces an upper bound on catalog and plan query duration.
- `lock_timeout = 3,000 ms` (3s): Prevents waiting in lock queues if concurrent DDL holds `ACCESS EXCLUSIVE`.
- Configured at connection handshake level across both CLI (`options`) and MCP (`server_settings`).

### Resilient Partial Audit (Fault Isolation)
Individual health checks execute in isolated sub-transactions:
- If a specific check encounters a timeout or query error, the failure is logged, recorded in `checks_failed` and `errors`, and `is_partial = True` is flagged on the report.
- Findings and evidence from all successfully completed checks are preserved intact.
- A partial audit is explicitly degraded and is never reported as clean/healthy. Global connection failures retain fatal exit semantics.

### Exit Codes (CI/CD Pipeline Ready)
| Code | Severity | Description |
|---|---|---|
| `0` | **OK** | All requested checks completed cleanly with zero findings and zero errors. |
| `1` | **ERROR** | Configuration error, invalid arguments, database connection failure, or fatal initialization error. |
| `2` | **WARNING** | Optimization findings present (redundant indexes, low HOT ratio, low-usage indexes, physical bloat, medium plan warnings), or degraded/partial audit execution without critical issues (`is_partial = True`). |
| `3` | **CRITICAL** | Urgent production risks detected (invalid indexes, unindexed foreign keys, excessive dead tuples, active blocking lock contention, high-risk plan bottlenecks), whether in a complete or partial audit run. |

---

## FastMCP Server Integration

The asynchronous MCP server (`mcp_pg_auditor.py`) is powered by `asyncpg` and `FastMCP`.

### Exposed Tools
- `pg_health_audit`: Full evaluation of up to 6 performance anti-patterns.
- `pg_locks`: Real-time inspection of lock contention and visual blocking tree.
- `pg_bloat`: Physical disk bloat estimation for tables and B-tree indexes.
- `pg_explain`: Automated query plan bottleneck analysis via passive `EXPLAIN (FORMAT JSON, COSTS)`.

### Client Configuration

#### Claude Desktop / Cursor (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "postgres-auditor": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "C:/Users/cerra/codigo/Herraminetas_Sql",
        "mcp_pg_auditor.py"
      ],
      "env": {
        "DATABASE_URL": "postgresql://user:password@localhost:5432/dbname"
      }
    }
  }
}
```

#### Running FastMCP in CLI Mode:
```bash
uv run mcp_pg_auditor.py --cli --dsn "postgresql://user:pass@localhost:5432/dbname"
```

---

## Remote Server & Tailscale Workflows

When inspecting databases running in remote containers or edge servers (e.g. Orange Pi / Docker):

```powershell
# 1. Establish an encrypted SSH tunnel via Tailscale
ssh -N -L 5545:127.0.0.1:5445 user@100.x.y.z

# 2. Run deterministic audit locally against the tunneled port
uv run python audit_pg.py --url "postgresql://user:password@127.0.0.1:5545/dbname"
```

---

## Sample Diagnostic Outputs

### 1. Storage & Health Audit
```text
=================================================================
        POSTGRESQL STORAGE & INDEX HEALTH REPORT
=================================================================

[1] INVALID INDEXES (indisvalid = false): 0
  -> OK: No invalid indexes detected.
[2] UNINDEXED FOREIGN KEYS: 0
  -> OK: No unindexed foreign keys detected.
[3] AUTOVACUUM & DEAD TUPLES LAG: 0
  -> OK: No tables with autovacuum lag or excessive dead tuples.
[4] REDUNDANT / PREFIX INDEXES FOUND: 2
  * Table: pedidos
    - Redundant: ix_pedidos_cliente_id (Size: 16 kB)
    - Covered by: idx_pedido_cliente
    - Redundant def: CREATE INDEX ix_pedidos_cliente_id ON public.pedidos USING btree (cliente_id)
    - Diagnostic: Index is a left-prefix duplicate covered by 'idx_pedido_cliente'.
    - Considerations: Consumes cache and write overhead redundantly; verify query usage and constraint requirements before planning index retirement.
[5] LOW HOT UPDATE EFFICIENCY: 0
  -> OK: No tables with low HOT update ratio.
[6] UNPROFITABLE / HIGH-WRITE LOW-READ INDEXES: 0
  -> OK: No unprofitable indexes detected.
=================================================================
```

### 2. Lock Contention Blocking Tree
```text
================================================================
POSTGRESQL LOCK CONTENTION: prod_db
Blocked Processes: 2 | Blocking Trees: 1
Max Wait Duration: 42.1s
================================================================

--- BLOCKING TREES ---
[PID 100] User: app_user | App: worker | State: idle in transaction
Wait Event: ClientRead | Lock Mode: ExclusiveLock | Duration: 45.2s
Query: UPDATE accounts SET balance = balance - 100 WHERE id = 1
  └── [PID 101] User: web_user | App: gunicorn | State: active
      Wait Event: transactionid | Lock Mode: ShareLock | Duration: 42.1s
      Query: UPDATE accounts SET balance = balance + 50 WHERE id = 1
        └── [PID 102] User: api_user | App: celery | State: active
            Wait Event: transactionid | Lock Mode: ShareLock | Duration: 12.0s
            Query: SELECT * FROM accounts WHERE id = 1 FOR UPDATE
```

### 3. Physical Bloat Estimation
```text
================================================================
POSTGRESQL PHYSICAL BLOAT ESTIMATION: prod_db
Bloated Tables: 1 | Bloated Indexes: 1
Total Estimated Bloat: 85.8 MB
================================================================

--- BLOATED TABLES (1) ---
- public.orders: 57.2 MB bloat (60.0%) | Real: 95.4 MB | Expected: 38.2 MB

--- BLOATED B-TREE INDEXES (1) ---
- public.idx_orders_customer (on orders): 28.6 MB bloat (50.0%) | Real: 57.2 MB | Expected: 28.6 MB
```

---

## Testing & Verification

The test suite covers the full domain contract, catalog queries, locks graph, bloat math, execution plan simulation, and differential drift:

```bash
# Run complete test suite (143 unit and integration tests)
uv run pytest

# Check code formatting and linting
uv run ruff check .

# Type checking (strict mode)
uv run mypy
```

---

## Historical Offline Migrations Boundary

> [!NOTE]
> The `scripts/migrations/` directory contains historical, offline DDL scripts created for specific database cleanups. These scripts are strictly isolated from the passive health auditor engine, are never presented as recommended actions, and are never executed by CLI or FastMCP runtime tools.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
