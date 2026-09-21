# Feature: sql-audit contexto de tabla (filas/tamaño) + umbral anti-falso-positivo

Mejora del check low-usage y HOT para que no reporten índices/HOT de tablas micro como problemas.

## Contexto (hallazgo probado en terreno — orangepi, Contador Oriental)

- `sql-audit` reportó 5 "índices de bajo uso" + 1 HOT en produccion real (EXIT=2).
- Validación con EXPLAIN en la BD real: las tablas son micro (expenses ~147 filas, ai_vector_memory ~1.084 filas). El planner elige Seq Scan + Sort aunque las búsquedas semánticas se ejecuten; los índices HNSW son prevención para cuando la tabla crezca → FALSOS POSITIVOS.
- El HOT de expenses (1.35%, 1.114 updates) viene del UPDATE de embedding (columna indexada → nunca HOT por diseño), no de fillfactor. En tabla de 147 filas es ruido.
- Lección: un índice con 0 scans en tabla de 147 filas ≠ índice muerto en tabla de 10M. El check debe incluir contexto de tamaño/filas y un umbral mínimo de filas.

## Cambios

1. **Modelos**: `LowUsageIndexIssue` y `HotUpdateIssue` ganan `table_rows: int` y `table_size: str`.
2. **Queries low-usage y HOT**: SELECT agrega `t.n_live_tup AS table_rows` y `pg_size_pretty(pg_total_relation_size(t.relid)) AS table_size`; filtro nuevo `AND t.n_live_tup >= %s`.
3. **Flag `--min-table-rows`** (default 10000): índice/HOT solo se reporta si la tabla tiene >= N filas. Aplica a low-usage y hot. Validación: entero > 0; si llega 0, error de uso.
4. **Render**: texto muestra `Rows: N | Size: X` en cada issue; JSON agrega keys `table_rows` y `table_size`.
5. **`--quiet`**: sin cambios (solo counts).
6. **Tests**: actualizar modelos existentes (8 campos), agregar: umbral excluye tabla chica (mock con n_live_tup bajo), incluye tabla grande, validación de flag inválida, JSON con contexto nuevo.
7. **README**: documentar `--min-table-rows` y contexto de tabla en tabla de flags y salida.
8. **Versión**: bump a 0.3.0.
9. Verificación: ruff + mypy strict + pytest + reinstalación del tool global (`uv tool install --force .` — si el wheel cacheado no se refresca, bump de version fuerza rebuild) + corrida real contra la orangepi (Contador Oriental) para confirmar que ya NO reporta los falsos positivos y que los hallazgos restantes tienen contexto.
10. Commit work-unit en branch feat/sql-audit-cli.

## Acceptance

- `sql-audit` contra la BD real del contador (tunnel SSH) NO reporta los 5 índices de tabla chica; los hallazgos HOT/low-usage que queden muestran `table_rows`/`table_size`.
- `--min-table-rows 1` restaura el comportamiento anterior (reporta todo).
- JSON con keys nuevas; mypy/ruff/pytest verdes.
- README documenta el flag.