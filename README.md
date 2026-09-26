# PostgreSQL Health Auditor (`sql-audit-mcp`)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![PostgreSQL](https://img.shields.io/badge/postgresql-14%20|%2015%20|%2016-336791.svg)](https://www.postgresql.org/)
[![FastMCP](https://img.shields.io/badge/MCP-FastMCP%20Server-green.svg)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Powered by uv](https://img.shields.io/badge/packaging-uv-DE5FE9.svg)](https://docs.astral.sh/uv/)
[![Tests Passing](https://img.shields.io/badge/tests-91%20passed-brightgreen.svg)](tests/)

A deterministic, zero-hallucination PostgreSQL health and performance auditor designed for production workloads. Runs both as a high-performance standalone CLI and as an on-demand [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for AI coding assistants (Antigravity, Cursor, Claude Desktop, Pi).

Catches silent performance bottlenecks, table-level locking hazards, dead tuple accumulation, broken HOT updates, physical disk bloat, and query execution bottlenecks in sub-second $O(1)$ catalog queries with zero LLM guesswork.

---

## Architectural Philosophy

- **Deterministic Inspection Over Stochastic Triage**: System catalogs (`pg_catalog`, `pg_stat_*`) provide authoritative database-state evidence. No LLM should "guess" whether an index is missing or redundant when PostgreSQL catalogs provide the exact answer in microseconds with zero VRAM overhead.
- **Canonical Domain Contract**: Standardized `AuditReport`, `Finding`, and `Evidence` schema with deterministic SHA-256 digests. Decouples identity (`finding_id`) from transient observation metrics (`evidence_id`), enabling reliable state tracking across time.
- **Differential Auditing (Drift Engine)**: Compares current inspection against historical baselines, surfacing `new`, `resolved`, `changed`, and `unchanged` findings to eliminate alert fatigue.
- **Zero-Downtime Remediation**: Automatically suggests and prepares safe, production-grade DDL migrations (`CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY`) without taking table-level exclusive locks.
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
Calculates expected vs. actual disk pages for tables and B-tree indexes based on column alignment, padding, and null bitmaps **without requiring the heavy `pgstattuple` extension**. Provides exact bloat percentages and bytes wasted.

### 3. Real-Time Locks & Blocking Tree (`--locks` / `pg_locks`)
Inspects active lock contention across `pg_locks` and `pg_stat_activity`. Reconstructs the recursive dependency graph, isolates root blocker PIDs, and renders an ASCII blocking tree with wait events and lock modes.

### 4. Query Plan Simulation & Bottleneck Detection (`--explain` / `pg_explain`)
Parses PostgreSQL JSON execution plans to detect sequential scans on large tables, nested loops with high row counts, sort/hash spills to disk (`work_mem` exhaustion), and plan cardinality misestimates. Supports `--analyze` with **safe automatic transaction rollback** (`BEGIN ... ROLLBACK`).

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

# 3. Filter by schemas and table thresholds
sql-audit --schema public,analytics --min-table-rows 1000 --min-size 10000000

# 4. Canonical JSON output (structured AuditReport contract)
sql-audit --canonical-json > baseline.json

# 5. Differential audit against baseline snapshot
sql-audit --diff baseline.json

# 6. Real-time lock contention & blocking tree
sql-audit --locks

# 7. Physical disk bloat estimation
sql-audit --bloat --min-bloat-bytes 10000000 --min-bloat-ratio 20.0

# 8. Query plan simulation (syntax / cost analysis)
sql-audit --explain "SELECT * FROM orders WHERE total > 100"

# 9. Live query plan execution with buffer stats (SAFE: automatic ROLLBACK)
sql-audit --explain-file query.sql --analyze

# 10. Quiet mode (summary counts only)
sql-audit --quiet
```

### Exit Codes (CI/CD Pipeline Ready)
| Code | Severity | Description |
|---|---|---|
| `0` | **OK** | All checks passed clean (or no active locks / clean bloat / safe query plan). |
| `1` | **ERROR** | Configuration error, invalid arguments, or database connection failure. |
| `2` | **WARNING** | Optimization findings present (redundant indexes, low HOT ratio, low-usage indexes, physical bloat, or medium plan warnings). |
| `3` | **CRITICAL** | Urgent production risks detected (invalid indexes, unindexed foreign keys, excessive dead tuples, active blocking lock contention, or high-risk plan bottlenecks). |

---

## FastMCP Server Integration

The asynchronous MCP server (`mcp_pg_auditor.py`) is powered by `asyncpg` and `FastMCP`.

### Exposed Tools
- `pg_health_audit`: Full evaluation of up to 6 performance anti-patterns.
- `pg_locks`: Real-time inspection of lock contention and visual blocking tree.
- `pg_bloat`: Physical disk bloat estimation for tables and B-tree indexes.
- `pg_explain`: Automated query plan bottleneck analysis with safe `EXPLAIN (ANALYZE, BUFFERS)` execution.

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
    - Suggestion: Consider DROP INDEX CONCURRENTLY ix_pedidos_cliente_id;
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
# Run complete test suite (91 unit and integration tests)
uv run pytest

# Check code formatting and linting
uv run ruff check .

# Type checking (strict mode)
uv run mypy
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
