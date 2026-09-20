# Herramientas SQL - Auditor de PostgreSQL

Auditor centralizado de salud de índices y almacenamiento en PostgreSQL. Detecta:
1. **Índices redundantes**: Índices idénticos o que son prefijos de otros índices compuestos.
2. **Problemas de HOT Updates**: Tablas con bajo ratio de actualización HOT y alerta de `fillfactor = 100%`.
3. **Índices con poco uso**: Índices con alto costo en escrituras pero casi nulas lecturas (`idx_scan`).

## Gestión con uv

Para instalar o sincronizar el entorno local:

```bash
uv sync
```

## Uso

El script incluye metadatos PEP 723, por lo que podés ejecutarlo directamente con `uv run` desde cualquier lugar sin tener que activar entornos manualmente.

### Opción 1: Desde la carpeta de cualquier proyecto (con archivo `.env`)
Te parás en la raíz del proyecto que querés auditar:

```bash
uv run C:\Users\cerra\codigo\Herraminetas_Sql\audit_pg.py
```
El script toma automáticamente la variable `DATABASE_URL` del `.env` local.

### Opción 2: Pasando la URL explícita por parámetro

```bash
uv run C:\Users\cerra\codigo\Herraminetas_Sql\audit_pg.py --url "postgresql://postgres:password@localhost:5432/mi_base"
```
