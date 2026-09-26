"""Canonical PostgreSQL catalog queries for sql-audit.

Used by both synchronous psycopg2 (CLI) and asynchronous asyncpg (MCP).
"""

# Query 1: Invalid Indexes
SQL_INVALID_INDEXES_SCHEMAS_PSYCOPG = """
SELECT
    c.relname AS child_table,
    idx.relname AS invalid_index,
    pg_size_pretty(pg_relation_size(i.indexrelid)) AS index_size
FROM pg_index i
JOIN pg_class idx ON idx.oid = i.indexrelid
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE i.indisvalid = FALSE
  AND n.nspname = ANY(%s)
ORDER BY pg_relation_size(i.indexrelid) DESC;
"""

SQL_INVALID_INDEXES_ALL_PSYCOPG = """
SELECT
    c.relname AS child_table,
    idx.relname AS invalid_index,
    pg_size_pretty(pg_relation_size(i.indexrelid)) AS index_size
FROM pg_index i
JOIN pg_class idx ON idx.oid = i.indexrelid
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE i.indisvalid = FALSE
  AND n.nspname NOT IN ('pg_catalog', 'pg_toast')
ORDER BY pg_relation_size(i.indexrelid) DESC;
"""

SQL_INVALID_INDEXES_ASYNCPG = """
SELECT
    c.relname AS child_table,
    idx.relname AS invalid_index,
    pg_size_pretty(pg_relation_size(i.indexrelid)) AS index_size
FROM pg_index i
JOIN pg_class idx ON idx.oid = i.indexrelid
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE i.indisvalid = FALSE
  AND n.nspname = ANY($1)
ORDER BY pg_relation_size(i.indexrelid) DESC;
"""

# Query 2: Unindexed Foreign Keys
SQL_UNINDEXED_FKS_SCHEMAS_PSYCOPG = """
SELECT
    c.conrelid::regclass::text AS child_table,
    c.conname AS fk_name,
    c.confrelid::regclass::text AS parent_table,
    pg_get_constraintdef(c.oid) AS fk_definition
FROM pg_constraint c
JOIN pg_namespace n ON n.oid = c.connamespace
WHERE c.contype = 'f'
  AND n.nspname = ANY(%s)
  AND NOT EXISTS (
    SELECT 1
    FROM pg_index i
    WHERE i.indrelid = c.conrelid
      AND (string_to_array(i.indkey::text, ' '))[1:cardinality(c.conkey)] =
          string_to_array(array_to_string(c.conkey, ' '), ' ')
      AND i.indisvalid
  )
ORDER BY c.conrelid::regclass::text, c.conname;
"""

SQL_UNINDEXED_FKS_ALL_PSYCOPG = """
SELECT
    c.conrelid::regclass::text AS child_table,
    c.conname AS fk_name,
    c.confrelid::regclass::text AS parent_table,
    pg_get_constraintdef(c.oid) AS fk_definition
FROM pg_constraint c
JOIN pg_namespace n ON n.oid = c.connamespace
WHERE c.contype = 'f'
  AND n.nspname NOT IN ('pg_catalog', 'pg_toast')
  AND NOT EXISTS (
    SELECT 1
    FROM pg_index i
    WHERE i.indrelid = c.conrelid
      AND (string_to_array(i.indkey::text, ' '))[1:cardinality(c.conkey)] =
          string_to_array(array_to_string(c.conkey, ' '), ' ')
      AND i.indisvalid
  )
ORDER BY c.conrelid::regclass::text, c.conname;
"""

SQL_UNINDEXED_FKS_ASYNCPG = """
SELECT
    c.conrelid::regclass::text AS child_table,
    c.conname AS fk_name,
    c.confrelid::regclass::text AS parent_table,
    pg_get_constraintdef(c.oid) AS fk_definition
FROM pg_constraint c
JOIN pg_namespace n ON n.oid = c.connamespace
WHERE c.contype = 'f'
  AND n.nspname = ANY($1)
  AND NOT EXISTS (
    SELECT 1
    FROM pg_index i
    WHERE i.indrelid = c.conrelid
      AND (string_to_array(i.indkey::text, ' '))[1:cardinality(c.conkey)] =
          string_to_array(array_to_string(c.conkey, ' '), ' ')
      AND i.indisvalid
  )
ORDER BY c.conrelid::regclass::text, c.conname;
"""

# Query 3: Autovacuum & Dead Tuples Lag
SQL_DEAD_TUPLES_SCHEMAS_PSYCOPG = """
SELECT
    st.relname AS table_name,
    st.n_dead_tup AS dead_tuples,
    st.n_live_tup AS live_tuples,
    ROUND(
        (st.n_dead_tup::numeric / NULLIF(st.n_live_tup + st.n_dead_tup, 0)) * 100, 2
    )::float AS dead_tuple_pct,
    st.last_autovacuum,
    st.last_vacuum
FROM pg_stat_user_tables st
WHERE st.schemaname = ANY(%s)
  AND st.n_dead_tup > 10000
  AND (st.n_dead_tup::numeric / NULLIF(st.n_live_tup + st.n_dead_tup, 0)) > 0.15
ORDER BY st.n_dead_tup DESC;
"""

SQL_DEAD_TUPLES_ALL_PSYCOPG = """
SELECT
    st.relname AS table_name,
    st.n_dead_tup AS dead_tuples,
    st.n_live_tup AS live_tuples,
    ROUND(
        (st.n_dead_tup::numeric / NULLIF(st.n_live_tup + st.n_dead_tup, 0)) * 100, 2
    )::float AS dead_tuple_pct,
    st.last_autovacuum,
    st.last_vacuum
FROM pg_stat_user_tables st
WHERE st.n_dead_tup > 10000
  AND (st.n_dead_tup::numeric / NULLIF(st.n_live_tup + st.n_dead_tup, 0)) > 0.15
ORDER BY st.n_dead_tup DESC;
"""

SQL_DEAD_TUPLES_ASYNCPG = """
SELECT
    st.relname AS table_name,
    st.n_dead_tup AS dead_tuples,
    st.n_live_tup AS live_tuples,
    ROUND(
        (st.n_dead_tup::numeric / NULLIF(st.n_live_tup + st.n_dead_tup, 0)) * 100, 2
    )::float AS dead_tuple_pct,
    st.last_autovacuum,
    st.last_vacuum
FROM pg_stat_user_tables st
WHERE st.schemaname = ANY($1)
  AND st.n_dead_tup > 10000
  AND (st.n_dead_tup::numeric / NULLIF(st.n_live_tup + st.n_dead_tup, 0)) > 0.15
ORDER BY st.n_dead_tup DESC;
"""

# Query 4: HOT & Fillfactor
SQL_HOT_PSYCOPG = """
SELECT
    t.relname AS table_name,
    t.n_tup_upd AS total_updates,
    t.n_tup_hot_upd AS hot_updates,
    ROUND(
        (t.n_tup_hot_upd::numeric / NULLIF(t.n_tup_upd, 0)) * 100, 2
    )::float AS hot_ratio_pct,
    COALESCE(
        (
            SELECT option_value::int
            FROM pg_options_to_table(c.reloptions)
            WHERE option_name = 'fillfactor'
        ), 100
    ) AS fillfactor,
    t.n_live_tup AS table_rows,
    pg_size_pretty(pg_total_relation_size(t.relid)) AS table_size
FROM pg_stat_user_tables t
JOIN pg_class c ON c.oid = t.relid
JOIN pg_namespace ns ON ns.oid = c.relnamespace
WHERE t.n_tup_upd >= %s
  AND ROUND((t.n_tup_hot_upd::numeric / NULLIF(t.n_tup_upd, 0)) * 100, 2) < %s
  AND t.n_live_tup >= %s
"""

SQL_HOT_ASYNCPG = """
SELECT
    t.relname AS table_name,
    t.n_tup_upd AS total_updates,
    t.n_tup_hot_upd AS hot_updates,
    ROUND(
        (t.n_tup_hot_upd::numeric / NULLIF(t.n_tup_upd, 0)) * 100, 2
    )::float AS hot_ratio_pct,
    COALESCE(
        (
            SELECT option_value::int
            FROM pg_options_to_table(c.reloptions)
            WHERE option_name = 'fillfactor'
        ), 100
    ) AS fillfactor,
    t.n_live_tup AS table_rows,
    pg_size_pretty(pg_total_relation_size(t.relid)) AS table_size
FROM pg_stat_user_tables t
JOIN pg_class c ON c.oid = t.relid
JOIN pg_namespace ns ON ns.oid = c.relnamespace
WHERE t.n_tup_upd >= $1
  AND ROUND((t.n_tup_hot_upd::numeric / NULLIF(t.n_tup_upd, 0)) * 100, 2) < $2
  AND t.n_live_tup >= $3
  AND ns.nspname = ANY($4)
ORDER BY t.n_tup_upd DESC;
"""

# Query 5: Redundant Indexes
SQL_REDUNDANT_PSYCOPG = """
WITH parsed_indexes AS (
    SELECT
        i.indexrelid,
        i.indrelid,
        i.indisunique,
        i.indisprimary,
        i.indpred,
        string_to_array(i.indkey::text, ' ') AS keys,
        pg_relation_size(i.indexrelid) AS size_bytes
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_am am ON am.oid = c.relam
    WHERE am.amname = 'btree'
      AND i.indisvalid
)
SELECT
    c.relname AS table_name,
    idx.relname AS redundant_index,
    pg_size_pretty(p1.size_bytes) AS redundant_size,
    lead_idx.relname AS covering_index,
    pg_get_indexdef(p1.indexrelid) AS redundant_def,
    pg_get_indexdef(p2.indexrelid) AS covering_def
FROM parsed_indexes p1
JOIN parsed_indexes p2 ON p1.indrelid = p2.indrelid AND p1.indexrelid != p2.indexrelid
JOIN pg_class idx ON idx.oid = p1.indexrelid
JOIN pg_class c ON c.oid = p1.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_class lead_idx ON lead_idx.oid = p2.indexrelid
WHERE n.nspname NOT IN ('pg_catalog', 'pg_toast')
  AND NOT p1.indisunique
  AND NOT p1.indisprimary
  AND p1.indpred IS NULL
  AND '0' != ALL(p1.keys)
  AND '0' != ALL(p2.keys)
  AND p2.keys[1:cardinality(p1.keys)] = p1.keys
  AND p1.size_bytes >= %s
"""

SQL_REDUNDANT_ASYNCPG = """
WITH parsed_indexes AS (
    SELECT
        i.indexrelid,
        i.indrelid,
        i.indisunique,
        i.indisprimary,
        i.indpred,
        string_to_array(i.indkey::text, ' ') AS keys,
        pg_relation_size(i.indexrelid) AS size_bytes
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_am am ON am.oid = c.relam
    WHERE am.amname = 'btree'
      AND i.indisvalid
)
SELECT
    c.relname AS table_name,
    idx.relname AS redundant_index,
    pg_size_pretty(p1.size_bytes) AS redundant_size,
    lead_idx.relname AS covering_index,
    pg_get_indexdef(p1.indexrelid) AS redundant_def,
    pg_get_indexdef(p2.indexrelid) AS covering_def
FROM parsed_indexes p1
JOIN parsed_indexes p2 ON p1.indrelid = p2.indrelid AND p1.indexrelid != p2.indexrelid
JOIN pg_class idx ON idx.oid = p1.indexrelid
JOIN pg_class c ON c.oid = p1.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_class lead_idx ON lead_idx.oid = p2.indexrelid
WHERE n.nspname = ANY($1)
  AND NOT p1.indisunique
  AND NOT p1.indisprimary
  AND p1.indpred IS NULL
  AND '0' != ALL(p1.keys)
  AND '0' != ALL(p2.keys)
  AND p2.keys[1:cardinality(p1.keys)] = p1.keys
  AND p1.size_bytes >= $2
ORDER BY p1.size_bytes DESC;
"""

# Query 6: Low Usage Indexes
SQL_LOW_USAGE_PSYCOPG = """
SELECT
    i.relname AS table_name,
    i.indexrelname AS index_name,
    pg_size_pretty(pg_relation_size(i.indexrelid)) AS size,
    i.idx_scan AS index_scans,
    (t.n_tup_ins + t.n_tup_upd + t.n_tup_del) AS table_writes,
    ROUND(
        i.idx_scan::numeric / NULLIF(t.n_tup_ins + t.n_tup_upd + t.n_tup_del, 0), 4
    ) AS read_write_ratio,
    t.n_live_tup AS table_rows,
    pg_size_pretty(pg_total_relation_size(t.relid)) AS table_size
FROM pg_stat_user_indexes i
JOIN pg_stat_user_tables t ON i.relid = t.relid
JOIN pg_index idx ON idx.indexrelid = i.indexrelid
JOIN pg_class c ON c.oid = i.relid
JOIN pg_namespace ns ON ns.oid = c.relnamespace
WHERE (t.n_tup_ins + t.n_tup_upd + t.n_tup_del) > 1000
  AND NOT idx.indisprimary
  AND NOT idx.indisunique
  AND (i.idx_scan::numeric / NULLIF(t.n_tup_ins + t.n_tup_upd + t.n_tup_del, 0)) < %s
  AND pg_relation_size(i.indexrelid) >= %s
  AND t.n_live_tup >= %s
"""

SQL_LOW_USAGE_ASYNCPG = """
SELECT
    c.relname AS table_name,
    i.relname AS index_name,
    pg_size_pretty(pg_relation_size(i.oid)) AS size,
    s.idx_scan AS index_scans,
    (t.n_tup_ins + t.n_tup_upd + t.n_tup_del) AS table_writes,
    ROUND(
        (s.idx_scan::numeric / NULLIF(t.n_tup_ins + t.n_tup_upd + t.n_tup_del, 0)),
        4
    )::float AS read_write_ratio,
    t.n_live_tup AS table_rows,
    pg_size_pretty(pg_total_relation_size(t.relid)) AS table_size
FROM pg_stat_user_indexes s
JOIN pg_stat_user_tables t ON t.relid = s.relid
JOIN pg_class c ON c.oid = s.relid
JOIN pg_class i ON i.oid = s.indexrelid
JOIN pg_index ix ON ix.indexrelid = s.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY($1)
  AND NOT ix.indisunique
  AND NOT ix.indisprimary
  AND (t.n_tup_ins + t.n_tup_upd + t.n_tup_del) > 1000
  AND (s.idx_scan::numeric / NULLIF(t.n_tup_ins + t.n_tup_upd + t.n_tup_del, 0)) <= $2
  AND pg_relation_size(i.oid) >= $3
  AND t.n_live_tup >= $4
ORDER BY pg_relation_size(i.oid) DESC;
"""
