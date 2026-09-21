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

- `uv tool install .` desde el repo → `sql-audit --help` funciona desde cualquier carpeta.
- `sql-audit` sin args detecta `.env` de un proyecto padre (walk-up).
- `sql-audit --json` emite JSON válido; exit code 2 cuando hay findings.
- `sql-audit --checks hot --schema public` corre solo el check pedido y filtra por schema.
- `uv run audit_pg.py` (PEP 723) sigue andando sin instalar.
- mypy strict y ruff limpios; tests verdes.