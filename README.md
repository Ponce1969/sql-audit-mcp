# PostgreSQL Health Auditor (`sql-audit-mcp`)

A deterministic, zero-hallucination PostgreSQL health and performance auditor. Operates as both a standalone CLI and an on-demand Model Context Protocol (MCP) server.

Designed to catch silent database performance killers, table-locking bottlenecks, and bloat in $O(1)$ by querying system catalogs (`pg_catalog` and `pg_stat_*`).

---

## Key Features

1. **Deterministic Catalog Inspection**: 100% deterministic rules. Zero tokens wasted on parsing or stochastic triage.
2. **6 Critical Performance Checks**:
   - **Invalid Indexes (`indisvalid = false`)**: Indexes aborted during `CREATE INDEX CONCURRENTLY` that incur write overhead without serving reads.
   - **Unindexed Foreign Keys**: Detects foreign keys lacking a B-tree index on their leading prefix (preventing `SHARE ROW EXCLUSIVE` table-level locks during parent `UPDATE`/`DELETE`).
   - **Autovacuum & Dead Tuples Lag**: Identifies table bloat and autovacuum starvation (`> 10,000` dead tuples and `> 15%` dead tuple ratio).
   - **HOT Updates Invalidation**: Finds high-update tables with poor Heap-Only Tuples efficiency (`< 30%`) and warns if `fillfactor = 100`.
   - **Redundant B-Tree Indexes**: Pinpoints duplicate indexes and prefix-redundant indexes using catalog array slicing.
   - **Low-Usage / Unprofitable Indexes**: Flags indexes with high write-maintenance overhead but negligible read scans (`idx_scan`).
3. **Zero-Noise On-Demand Architecture**: Built to be summoned only when needed by LLM agents (Antigravity, Pi, Cursor, Claude Desktop), avoiding prompt context bloat.
4. **Zero-Trust Credential Security**: Connection strings (`DATABASE_URL`) are resolved from the environment/server side, never leaked over the MCP JSON-RPC protocol.

---

## Quickstart

### Prerequisites
- Python >= 3.10
- [`uv`](https://docs.astral.sh/uv/) (recommended for zero-setup execution)

### 1. Standalone CLI Execution (via `uv`)

Run ad-hoc without installing virtual environments (uses PEP 723 metadata):

```bash
# Using explicit connection string
uv run mcp_pg_auditor.py --cli --dsn "postgresql://user:password@localhost:5432/dbname"

# Using DATABASE_URL from .env
uv run mcp_pg_auditor.py --cli

# Export formatted JSON report
uv run mcp_pg_auditor.py --cli --json
```

#### CLI Options:
| Flag | Description | Default |
|---|---|---|
| `--dsn` | Direct PostgreSQL connection string | `$DATABASE_URL` |
| `--alias` | Connection alias to resolve `DB_<ALIAS>_URL` | `default` |
| `--schemas` | Comma-separated schemas to audit | `public` |
| `--min-table-rows` | Minimum live rows to evaluate (eliminates noise on small tables) | `1000` |
| `--min-size-bytes` | Minimum index size in bytes for redundancy checks | `10000000` (10 MB) |
| `--json` | Output full structured report as JSON | Disabled |

---

## Model Context Protocol (MCP) Server

Exposes the tool `pg_health_audit` for AI coding agents.

### Tool Definition
- **Tool Name**: `pg_health_audit`
- **Parameters**:
  - `schemas` (list of strings, default `["public"]`)
  - `enabled_checks` (optional list of check names)
  - `min_table_rows` (integer, default `1000`)
  - `min_size_bytes` (integer, default `10000000`)
  - `db_alias` (string, default `"default"`)

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
        "/path/to/Herraminetas_Sql",
        "mcp_pg_auditor.py"
      ],
      "env": {
        "DATABASE_URL": "postgresql://user:password@localhost:5432/dbname"
      }
    }
  }
}
```

#### Pi Agent (`~/.pi/agent/mcp.json`):
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

---

## Example Output

```text
=======================================================
 PostgreSQL Health Audit Report (default)
 Execution time: 868.64 ms | Schemas: public
 Critical issues detected: YES
=======================================================

[1] Invalid Indexes (indisvalid = false): 0
[2] Unindexed Foreign Keys: 2
    - FK: entrega_eventos_usuario_id_fkey on entrega_eventos -> usuarios
      Def: FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    - FK: pagos_registrado_por_fkey on pagos -> usuarios
      Def: FOREIGN KEY (registrado_por) REFERENCES usuarios(id)
[3] Autovacuum & Dead Tuples Lag: 0
[4] Broken HOT Updates / Fillfactor Issues: 0
[5] Redundant / Duplicate Indexes: 0
[6] Low Usage / Unprofitable Indexes: 0

Audit completed.
```

---

## License

MIT License.
