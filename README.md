# Herramientas SQL - Auditor de PostgreSQL (`sql-audit`)

Auditor centralizado de salud de índices y almacenamiento en PostgreSQL. Detecta:

1. **Índices redundantes**: índices idénticos o que son prefijos de otros índices compuestos.
2. **Problemas de HOT Updates**: tablas con bajo ratio de actualización HOT y alerta de `fillfactor = 100%`.
3. **Índices con poco uso**: índices con alto costo en escrituras pero casi nulas lecturas (`idx_scan`).

## Instalación global

El paquete expone el comando `sql-audit`. Para instalarlo de forma global con `uv`:

```bash
uv tool install .
```

Después de la instalación podés ejecutarlo desde cualquier proyecto:

```bash
cd /ruta/de/cualquier/proyecto
sql-audit
```

Si querés desinstalarlo:

```bash
uv tool uninstall herraminetas-sql
```

## Uso

```bash
sql-audit [opciones]
```

### Sin instalación (ad-hoc con PEP 723)

El script `audit_pg.py` incluye metadatos PEP 723, así que también podés ejecutarlo
directamente con `uv run` sin instalar nada:

```bash
uv run audit_pg.py
```

### Resolución de configuración

`sql-audit` resuelve la conexión a la base de datos en este orden de prioridad:

1. `--url <cadena>` (flag explícito).
2. `--env-file <ruta>` (archivo `.env` explícito con `DATABASE_URL`).
3. Archivo `.env` descubierto **caminando hacia arriba** desde el directorio actual.
4. Variables de entorno estándar de libpq: `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`.
5. Variables estilo docker-compose `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` (compatibilidad: muchos `.env` de proyectos reales las usan).

Si no se encuentra ninguna configuración, el comando termina con código de salida `1`
y un mensaje en stderr.

### Tabla de flags

| Flag | Descripción | Default |
|------|-------------|---------|
| `--url` | Cadena de conexión PostgreSQL. | — |
| `--env-file` | Ruta a un archivo `.env` con `DATABASE_URL`. | — |
| `--checks` | Subconjunto de checks separado por comas: `redundant`, `hot`, `low-usage`. | todos |
| `--schema` | Esquema (namespace) para filtrar. Repetible o separado por comas. | todos |
| `--min-size` | Ignora índices menores a este tamaño en bytes (checks `redundant` y `low-usage`). | `0` |
| `--json` | Emite un reporte JSON a stdout. | — |
| `--quiet` | Imprime solo los conteos del resumen (modo texto). | — |
| `--timeout` | Timeout de conexión en segundos. | `10` |
| `--min-hot-ratio` | Ratio HOT mínimo aceptable (porcentaje). | `30.0` |
| `--min-updates` | Actualizaciones mínimas para considerar una tabla en el análisis HOT. | `50` |
| `--max-rw-ratio` | Ratio lectura/escritura máximo para marcar un índice como de poco uso. | `0.05` |
| `--min-table-rows` | Solo reporta issues de `hot` y `low-usage` para tablas con al menos N filas vivas; `0` desactiva el umbral. | `10000` |

### Códigos de salida

| Código | Significado |
|--------|-------------|
| `0` | La auditoría corrió y no encontró problemas. |
| `1` | Error (configuración no encontrada, fallo de conexión o error no manejado). |
| `2` | La auditoría corrió y encontró al menos un problema. |

### Salida JSON

Con `--json` el reporte se emite como un objeto JSON estable en stdout. La conexión
nunca se muestra completa: el campo `database` contiene solo el host (sin usuario ni
contraseña).

```bash
sql-audit --json
```

```json
{
  "database": "db.example.com",
  "checks": ["redundant", "hot", "low-usage"],
  "summary": {
    "redundant_indexes": 1,
    "hot_issues": 0,
    "low_usage_indexes": 2
  },
  "issues": {
    "redundant": [
      {
        "table_name": "users",
        "redundant_index": "idx_users_email",
        "redundant_size": "16 kB",
        "covering_index": "idx_users_email_id",
        "redundant_def": "CREATE INDEX ...",
        "covering_def": "CREATE INDEX ..."
      }
    ],
    "hot": [],
    "low-usage": [
      {
        "table_name": "events",
        "index_name": "idx_events_created_at",
        "size": "32 kB",
        "index_scans": 3,
        "table_writes": 5000,
        "read_write_ratio": 0.0006,
        "table_rows": 50000,
        "table_size": "12 MB"
      }
    ]
  }
}
```

Los issues `hot` y `low-usage` incluyen además el contexto de tabla `table_rows` (filas
vivas estimadas) y `table_size` (tamaño total de la tabla), para distinguir tablas micro
(ruido) de hallazgos reales.

## Desarrollo

Para sincronizar el entorno de desarrollo (incluye pytest, ruff y mypy):

```bash
uv sync
```

Para correr los tests y los linters:

```bash
uv run --group dev pytest tests -q
uv run --group dev ruff check audit_pg.py tests
uv run --group dev mypy audit_pg.py
```
