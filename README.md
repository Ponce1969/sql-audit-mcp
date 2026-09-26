# PostgreSQL Health Auditor (`sql-audit-mcp`)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![PostgreSQL](https://img.shields.io/badge/postgresql-14%20|%2015%20|%2016-336791.svg)](https://www.postgresql.org/)
[![FastMCP](https://img.shields.io/badge/MCP-FastMCP%20Server-green.svg)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Powered by uv](https://img.shields.io/badge/packaging-uv-DE5FE9.svg)](https://docs.astral.sh/uv/)

A deterministic, zero-hallucination PostgreSQL health and performance auditor designed for production workloads. Runs both as an installable standalone CLI and as an on-demand [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for AI coding assistants (Antigravity, Pi, Cursor, Claude Desktop).

Catches silent performance bottlenecks, table-level locking hazards, dead tuple accumulation, and broken HOT updates in sub-second $O(1)$ catalog queries designed for low-overhead inspection suitable for production environments.

---

## Architectural Philosophy

- **Deterministic Inspection Over Stochastic Triage**: We query system catalogs (`pg_catalog`, `pg_stat_*`) directly. PostgreSQL system catalogs and statistics views provide authoritative database-state evidence for the checks they expose. No LLM should "guess" whether an index is missing or redundant when PostgreSQL catalogs provide the exact answer in microseconds with zero VRAM overhead.
- **AI as Remediation, Not Ingestion**: The AI assistant (or human DBA) receives structured, verified diagnostics and focuses on reasoning: generating zero-downtime DDL (`CREATE INDEX CONCURRENTLY`), reviewing application queries, and planning migration sequences.
- **Zero-Noise On-Demand Design**: MCP tool schemas are heavy. When idle, this server injects zero tokens into your agent's context. It is triggered only upon explicit diagnostic intent (`/sql-audit` or "audit database").
- **Zero-Trust Credential Security**: Credentials and connection strings are resolved strictly on the host/server side (`DATABASE_URL` or `.env`). Database passwords never travel over JSON-RPC protocols or touch LLM context windows.

---

## The 6 Critical Performance Checks

| # | Check Name | Target Catalog / Metric | Operational Impact in Production |
|---|---|---|---|
| **1** | **Invalid Indexes** | `pg_index.indisvalid = false` | Aborted `CREATE INDEX CONCURRENTLY` leaves unusable index structures. Every `INSERT`/`UPDATE` pays write penalty, while planner ignores it for reads. |
| **2** | **Unindexed Foreign Keys** | `pg_constraint` vs `pg_index` left-prefix slice | Missing B-Tree index on referencing column forces sequential scans and acquires heavy `SHARE ROW EXCLUSIVE` table-level locks during parent `UPDATE`/`DELETE`. |
| **3** | **Autovacuum & Dead Tuples Lag** | `pg_stat_user_tables.n_dead_tup` | Identifies tables with `> 10,000` dead tuples and `> 15%` dead tuple pressure where autovacuum is blocked or lagging, destroying cache locality and table density. |
| **4** | **Broken HOT Updates** | `n_tup_hot_upd / n_tup_upd < 30%` & `fillfactor` | High-update tables failing Heap-Only Tuple optimization. Alerts when default `fillfactor = 100` prevents in-page updates and generates excessive WAL traffic. |
| **5** | **Redundant B-Tree Indexes** | CTE prefix matching `p2.keys[1:len(p1)] = p1.keys` | Detects duplicate indexes and prefix-redundant indexes strictly for B-Tree (`amname = 'btree'`), saving disk, memory cache, and write I/O. |
| **6** | **Low-Usage / Unprofitable Indexes** | `idx_scan / (writes) < 0.05` | Indexes with high maintenance write overhead and negligible read lookups on tables with `> 1,000` writes. |

---

## Installation & Packaging

### Standalone CLI (via `uv`)

You can install `sql-audit` globally or run it ad-hoc without manual virtual environment management:

```bash
# Global tool installation
uv tool install .

# Or run ad-hoc with PEP 723 metadata (zero installation required)
uv run audit_pg.py --url "postgresql://user:pass@localhost:5432/dbname"
```

### With MCP Dependencies

```bash
# Install with optional MCP dependencies
uv pip install -e ".[mcp]"
```

---

## Usage

### 1. Standalone CLI (`audit_pg.py` / `sql-audit`)

```bash
# Basic run with automatic .env discovery
sql-audit

# Explicit connection string
sql-audit --url "postgresql://user:password@localhost:5432/dbname"

# Filter by schemas and table thresholds
sql-audit --schema public,analytics --min-table-rows 1000 --min-size 10000000

# JSON output for machine consumption
sql-audit --json

# Canonical JSON output (structured AuditReport contract)
sql-audit --canonical-json

# Differential audit against a previous baseline report
sql-audit --diff previous_audit.json

# Real-time lock contention & blocking tree inspection
sql-audit --locks

# Physical disk bloat estimation for tables and B-tree indexes
sql-audit --bloat --min-bloat-bytes 10000000 --min-bloat-ratio 20.0

# Query plan bottleneck simulation and audit
sql-audit --explain "SELECT * FROM orders WHERE total > 100"

# Safe execution audit with buffer & runtime statistics (automatically rolled back)
sql-audit --explain-file query.sql --analyze

# Quiet mode (summary counts only)
sql-audit --quiet
```

#### Exit Codes (CI / Pipeline Ready)
- `0`: All checks passed. Database is healthy (or clean locks / no bloat / clean query plan).
- `1`: Configuration error or connection failure.
- `2`: Optimization findings detected (redundant indexes, low HOT ratio, low-usage indexes, physical bloat, or medium plan warnings).
- `3`: **Critical issues detected** (invalid indexes, unindexed foreign keys, excessive dead tuple bloat, active blocking lock contention, or high-risk plan bottlenecks like large seq scans / disk spills).

---

### 2. FastMCP Server (`mcp_pg_auditor.py` / `sql-audit-mcp`)

The MCP server exposes four deterministic tools:
- `pg_health_audit`: Full evaluation of up to 6 performance anti-patterns.
- `pg_locks`: Real-time inspection of lock contention with recursive blocking tree reconstruction.
- `pg_bloat`: Statistical physical disk bloat estimation for tables and B-tree indexes without requiring `pgstattuple`.
- `pg_explain`: Automated audit of PostgreSQL execution plans with bottleneck detection and safe transactional `EXPLAIN ANALYZE` rollback.

#### Client Configuration Examples

##### Claude Desktop / Cursor (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "postgres-auditor": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/sql-audit-mcp",
        "mcp_pg_auditor.py"
      ],
      "env": {
        "DATABASE_URL": "postgresql://user:password@localhost:5432/dbname"
      }
    }
  }
}
```

##### Pi Agent (`~/.pi/agent/mcp.json`):
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
      "cwd": "C:/Users/cerra/codigo/Herraminetas_Sql"
    }
  }
}
```

#### Running FastMCP in CLI Mode
You can also execute the async FastMCP engine directly from the command line:

```bash
uv run mcp_pg_auditor.py --cli --dsn "postgresql://user:pass@localhost:5432/dbname"
```

---

## Sample Diagnostic Output

```text
=================================================================
        POSTGRESQL STORAGE & INDEX HEALTH REPORT
=================================================================

[1] INVALID INDEXES (indisvalid = false): 0
  -> OK: No invalid indexes detected.

[2] UNINDEXED FOREIGN KEYS: 2
  * Table: entrega_eventos -> usuarios
    - FK: entrega_eventos_usuario_id_fkey
    - Definition: FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    - Suggestion: Consider CREATE INDEX CONCURRENTLY to prevent table locks.

  * Table: pagos -> usuarios
    - FK: pagos_registrado_por_fkey
    - Definition: FOREIGN KEY (registrado_por) REFERENCES usuarios(id)
    - Suggestion: Consider CREATE INDEX CONCURRENTLY to prevent table locks.

[3] AUTOVACUUM & DEAD TUPLES LAG: 0
  -> OK: No tables with autovacuum lag or excessive dead tuples.

[4] REDUNDANT / PREFIX INDEXES FOUND: 0
  -> OK: No redundant indexes detected.

[5] LOW HOT UPDATE EFFICIENCY: 0
  -> OK: No tables with low HOT update ratio.

[6] UNPROFITABLE / HIGH-WRITE LOW-READ INDEXES: 0
  -> OK: No unprofitable indexes detected.
=================================================================
```

---

## Testing & Code Quality

The project adheres to strict type annotations (`mypy --strict`) and high code style standards (`ruff`):

```bash
# Run test suite (67 unit and integration tests)
uv run pytest

# Check code formatting and linting
uv run ruff check .

# Type checking
uv run mypy
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
