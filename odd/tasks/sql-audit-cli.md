# Feature: sql-audit CLI

Convierte `audit_pg.py` en una herramienta CLI instalable globalmente, usable desde cualquier proyecto para auditar problemas comunes de PostgreSQL.

## Contexto

- Repo: `C:/Users/cerra/codigo/Herraminetas_Sql` (branch `master`)
- Hoy: script único con PEP 723, se corre con `uv run C:\...\audit_pg.py` (ruta completa).
- Objetivo: comando `sql-audit` en PATH (`uv tool install .`), config descubierta automáticamente, salida para humanos y para CI, filtros de alcance.

## Tareas

1. **Empaquetado CLI**: `[project.scripts] sql-audit = "audit_pg:main"` en `pyproject.toml` + `uv tool install .` funciona. PEP 723 sigue funcionando para uso ad-hoc.
2. **Config robusta**: `.env` descubierto caminando hacia arriba (`find_dotenv`), flag `--env-file`, fallback a variables libpq nativas (`PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE`). Orden: `--url` > `--env-file` > `.env` walk-up > libpq env vars.
3. **Salida automatizable**: `--json`, `--quiet`, exit codes (0 = sin issues, 1 = error de ejecución, 2 = issues encontrados).
4. **Filtros**: `--checks redundant,hot,low-usage`, `--schema`, `--min-size` + `connect_timeout`.
5. **Refactor**: dataclasses en vez de pydantic; actualizar header PEP 723 y deps de pyproject (sacar pydantic). `main` retorna exit code y es testeable vía inyección.
6. **Tests** (pytest): resolución de config, parsing de args, rendering JSON/texto, exit codes. Sin DB real — inyección de auditor fake.
7. **README**: instalación global, tabla de flags, exit codes, formato JSON, orden de resolución de config.
8. **Verificación**: `ruff check`, `mypy --strict`, `pytest -q`, instalación global verificada desde otra carpeta.
9. **Commits** por unidad de trabajo en feature branch `feat/sql-audit-cli` (conventional commits, tests y docs junto al código).

## Acceptance

- `uv tool install .` desde el repo → `sql-audit --help` funciona desde cualquier carpeta. ✅ verificado
- `sql-audit` sin args detecta `.env` de un proyecto padre (walk-up). ✅ verificado desde subdir anidado
- `sql-audit --json` emite JSON válido; exit code 2 cuando hay findings. ✅ tests
- `sql-audit --checks hot --schema public` corre solo el check pedido y filtra por schema. ✅ tests
- `uv run audit_pg.py` (PEP 723) sigue andando sin instalar. ✅ verificado
- mypy strict y ruff limpios; tests verdes. ✅ 24 passed

## Evidencia de commits (branch `feat/sql-audit-cli`)

- `e27d169` feat: add installable sql-audit CLI with config discovery, JSON output, and exit codes
- `51a0fac` docs: document sql-audit installation, flags, exit codes and JSON output

## Estado final

- Instalado globalmente con `uv tool install .` (executable `sql-audit`). Desinstalar: `uv tool uninstall herraminetas-sql`.
- `.gitignore` quedó sin commitear (cambio pre-sesión: agrega `.atl/`).
- Push/PR quedan como decisión del usuario.
- Riesgo conocido: los filtros SQL (`--schema`, `--min-size`) no probados contra una BD real todavía.

## Mejora v0.3.0 — contexto de tabla + umbral anti-falso-positivo (commit 96b448a)

- Hallazgo probado en produccion: los 5 low-usage + 1 HOT eran falsos positivos por tablas micro (expenses 147 filas, ai_vector_memory 1.084). EXPLAIN confirmó Seq Scan del planner pese a que las búsquedas semánticas se ejecutan.
- Fix: HotUpdateIssue/LowUsageIndexIssue + table_rows/table_size; queries filtran `t.n_live_tup >= min_table_rows`; flag `--min-table-rows` (default 10000, 0 desactiva); render texto/JSON con contexto; bump 0.3.0; 28 tests.
- Aceptación en produccion cumplida: default → EXIT=0 sin falsos positivos; `--min-table-rows 0` → EXIT=2 con contexto (table_rows 156, table_size 1552 kB).

## Prueba en terreno — Contador Oriental (C:/Users/cerra/codigo/flet)

- BD real: pgvector pg16 en Docker (`auditor_familiar_db`, 127.0.0.1:5432), 25 tablas, 62 índices.
- `sql-audit` sin flags desde el proyecto: conecta vía walk-up `.env` + fallback `POSTGRES_*`, corre las 3 auditorías.
- Hallazgo: el `.env` del contador usa `POSTGRES_*` (patrón docker-compose) y el refactor a libpq `PG*` lo había roto → fix `2486c33`: fallback `_build_postgres_env_url()` tras libpq, bump 0.2.1, +2 tests.
- Advertencia de interpretación: pg_stat_* se resetea al reiniciar el container (escribas totales = 0) → el 0 en HOT/low-usage es por falta de datos acumulados, no un veredicto. El 0 en redundantes sí es hallazgo real (catálogo puro).
- Gotcha uv: `uv tool install --force .` reusó wheel caché (misma versión) → se reinstaló stale. Solución: bump de versión (0.2.1).