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

# Query 7: Lock Contention and Blocking Graph
SQL_LOCK_CONTENTION = """
SELECT
    blocked.pid AS blocked_pid,
    COALESCE(blocked.usename, '') AS blocked_user,
    COALESCE(blocked.application_name, '') AS blocked_app,
    COALESCE(blocked.client_addr::text, '') AS blocked_client_addr,
    EXTRACT(EPOCH FROM (now() - blocked.query_start))::float AS blocked_duration_sec,
    EXTRACT(EPOCH FROM (now() - blocked.xact_start))::float AS blocked_xact_age_sec,
    COALESCE(blocked.wait_event_type, '') AS blocked_wait_event_type,
    COALESCE(blocked.wait_event, '') AS blocked_wait_event,
    COALESCE(blocked.state, '') AS blocked_state,
    COALESCE(blocked.query, '') AS blocked_query,
    blocking_pids.blocking_pid,
    COALESCE(blocking.usename, '') AS blocking_user,
    COALESCE(blocking.application_name, '') AS blocking_app,
    COALESCE(blocking.client_addr::text, '') AS blocking_client_addr,
    EXTRACT(EPOCH FROM (now() - blocking.query_start))::float AS blocking_duration_sec,
    EXTRACT(EPOCH FROM (now() - blocking.xact_start))::float AS blocking_xact_age_sec,
    COALESCE(blocking.state, '') AS blocking_state,
    COALESCE(blocking.wait_event_type, '') AS blocking_wait_event_type,
    COALESCE(blocking.wait_event, '') AS blocking_wait_event,
    COALESCE(blocking.query, '') AS blocking_query
FROM pg_stat_activity blocked
CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS blocking_pids(blocking_pid)
JOIN pg_stat_activity blocking ON blocking.pid = blocking_pids.blocking_pid
WHERE NOT blocked.pid = pg_backend_pid()
ORDER BY blocked_duration_sec DESC;
"""

# Query 8: Physical Table Bloat Estimation
SQL_TABLE_BLOAT_ALL_PSYCOPG = """
WITH constants AS (
    SELECT
        current_setting('block_size')::numeric AS bs,
        24 AS page_hdr,
        24 AS tpl_hdr,
        8 AS ma
),
table_stats AS (
    SELECT
        n.nspname AS schema_name,
        c.relname AS table_name,
        c.oid AS table_oid,
        c.relpages,
        c.reltuples,
        COALESCE(
            1 + COUNT(s.attname) / 8,
            0
        ) AS null_hdr,
        COALESCE(
            SUM(
                (1.0 - COALESCE(s.null_frac, 0.0)) * COALESCE(s.avg_width, 1024)
            ),
            1024
        ) AS data_width
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_stats s ON s.schemaname = n.nspname AND s.tablename = c.relname
    WHERE c.relkind IN ('r', 'm')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND c.relpages > 0
    GROUP BY n.nspname, c.relname, c.oid, c.relpages, c.reltuples
),
table_est AS (
    SELECT
        ts.schema_name,
        ts.table_name,
        ts.relpages,
        ts.reltuples,
        k.bs,
        (k.bs * ts.relpages)::bigint AS table_size_bytes,
        CEIL(
            ts.reltuples / NULLIF(
                FLOOR(
                    (k.bs - k.page_hdr) /
                    NULLIF(
                        CEIL((k.tpl_hdr + ts.null_hdr + ts.data_width)::numeric / k.ma) * k.ma + 4,
                        0
                    )
                ),
                0
            )
        )::bigint AS expected_pages
    FROM table_stats ts
    CROSS JOIN constants k
)
SELECT
    schema_name,
    table_name,
    table_size_bytes,
    (expected_pages * bs)::bigint AS expected_size_bytes,
    GREATEST(0, (table_size_bytes - (expected_pages * bs)::bigint)) AS bloat_bytes,
    ROUND(
        (
            GREATEST(0, (table_size_bytes - (expected_pages * bs)::bigint))::numeric
            / NULLIF(table_size_bytes, 0)
            * 100
        )::numeric,
        2
    )::float AS bloat_ratio_pct
FROM table_est
ORDER BY bloat_bytes DESC;
"""

SQL_TABLE_BLOAT_SCHEMAS_PSYCOPG = """
WITH constants AS (
    SELECT
        current_setting('block_size')::numeric AS bs,
        24 AS page_hdr,
        24 AS tpl_hdr,
        8 AS ma
),
table_stats AS (
    SELECT
        n.nspname AS schema_name,
        c.relname AS table_name,
        c.oid AS table_oid,
        c.relpages,
        c.reltuples,
        COALESCE(
            1 + COUNT(s.attname) / 8,
            0
        ) AS null_hdr,
        COALESCE(
            SUM(
                (1.0 - COALESCE(s.null_frac, 0.0)) * COALESCE(s.avg_width, 1024)
            ),
            1024
        ) AS data_width
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_stats s ON s.schemaname = n.nspname AND s.tablename = c.relname
    WHERE c.relkind IN ('r', 'm')
      AND n.nspname = ANY(%s)
      AND c.relpages > 0
    GROUP BY n.nspname, c.relname, c.oid, c.relpages, c.reltuples
),
table_est AS (
    SELECT
        ts.schema_name,
        ts.table_name,
        ts.relpages,
        ts.reltuples,
        k.bs,
        (k.bs * ts.relpages)::bigint AS table_size_bytes,
        CEIL(
            ts.reltuples / NULLIF(
                FLOOR(
                    (k.bs - k.page_hdr) /
                    NULLIF(
                        CEIL((k.tpl_hdr + ts.null_hdr + ts.data_width)::numeric / k.ma) * k.ma + 4,
                        0
                    )
                ),
                0
            )
        )::bigint AS expected_pages
    FROM table_stats ts
    CROSS JOIN constants k
)
SELECT
    schema_name,
    table_name,
    table_size_bytes,
    (expected_pages * bs)::bigint AS expected_size_bytes,
    GREATEST(0, (table_size_bytes - (expected_pages * bs)::bigint)) AS bloat_bytes,
    ROUND(
        (
            GREATEST(0, (table_size_bytes - (expected_pages * bs)::bigint))::numeric
            / NULLIF(table_size_bytes, 0)
            * 100
        )::numeric,
        2
    )::float AS bloat_ratio_pct
FROM table_est
ORDER BY bloat_bytes DESC;
"""

SQL_TABLE_BLOAT_ASYNCPG = """
WITH constants AS (
    SELECT
        current_setting('block_size')::numeric AS bs,
        24 AS page_hdr,
        24 AS tpl_hdr,
        8 AS ma
),
table_stats AS (
    SELECT
        n.nspname AS schema_name,
        c.relname AS table_name,
        c.oid AS table_oid,
        c.relpages,
        c.reltuples,
        COALESCE(
            1 + COUNT(s.attname) / 8,
            0
        ) AS null_hdr,
        COALESCE(
            SUM(
                (1.0 - COALESCE(s.null_frac, 0.0)) * COALESCE(s.avg_width, 1024)
            ),
            1024
        ) AS data_width
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_stats s ON s.schemaname = n.nspname AND s.tablename = c.relname
    WHERE c.relkind IN ('r', 'm')
      AND n.nspname = ANY($1)
      AND c.relpages > 0
    GROUP BY n.nspname, c.relname, c.oid, c.relpages, c.reltuples
),
table_est AS (
    SELECT
        ts.schema_name,
        ts.table_name,
        ts.relpages,
        ts.reltuples,
        k.bs,
        (k.bs * ts.relpages)::bigint AS table_size_bytes,
        CEIL(
            ts.reltuples / NULLIF(
                FLOOR(
                    (k.bs - k.page_hdr) /
                    NULLIF(
                        CEIL((k.tpl_hdr + ts.null_hdr + ts.data_width)::numeric / k.ma) * k.ma + 4,
                        0
                    )
                ),
                0
            )
        )::bigint AS expected_pages
    FROM table_stats ts
    CROSS JOIN constants k
)
SELECT
    schema_name,
    table_name,
    table_size_bytes,
    (expected_pages * bs)::bigint AS expected_size_bytes,
    GREATEST(0, (table_size_bytes - (expected_pages * bs)::bigint)) AS bloat_bytes,
    ROUND(
        (
            GREATEST(0, (table_size_bytes - (expected_pages * bs)::bigint))::numeric
            / NULLIF(table_size_bytes, 0)
            * 100
        )::numeric,
        2
    )::float AS bloat_ratio_pct
FROM table_est
ORDER BY bloat_bytes DESC;
"""

# Query 9: Physical B-Tree Index Bloat Estimation
SQL_INDEX_BLOAT_ALL_PSYCOPG = """
WITH constants AS (
    SELECT
        current_setting('block_size')::numeric AS bs,
        40 AS page_hdr,
        8 AS tpl_hdr,
        8 AS ma
),
index_stats AS (
    SELECT
        n.nspname AS schema_name,
        c.relname AS table_name,
        i.relname AS index_name,
        i.relpages,
        i.reltuples,
        COALESCE(
            SUM(
                (1.0 - COALESCE(s.null_frac, 0.0)) * COALESCE(s.avg_width, 8)
            ),
            16
        ) AS data_width
    FROM pg_class i
    JOIN pg_index x ON x.indexrelid = i.oid
    JOIN pg_class c ON c.oid = x.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_am am ON am.oid = i.relam AND am.amname = 'btree'
    LEFT JOIN pg_stats s ON s.schemaname = n.nspname
                        AND s.tablename = c.relname
                        AND s.attname = ANY(
                            SELECT a.attname
                            FROM pg_attribute a
                            WHERE a.attrelid = c.oid
                              AND a.attnum = ANY(string_to_array(x.indkey::text, ' ')::int[])
                        )
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND i.relpages > 0
    GROUP BY n.nspname, c.relname, i.relname, i.relpages, i.reltuples
),
index_est AS (
    SELECT
        is_s.schema_name,
        is_s.table_name,
        is_s.index_name,
        is_s.relpages,
        is_s.reltuples,
        k.bs,
        (k.bs * is_s.relpages)::bigint AS index_size_bytes,
        CEIL(
            (is_s.reltuples * (
                CEIL((k.tpl_hdr + is_s.data_width)::numeric / k.ma) * k.ma + 4
            )) / NULLIF((k.bs - k.page_hdr) * 0.70, 0)
        )::bigint AS expected_pages
    FROM index_stats is_s
    CROSS JOIN constants k
)
SELECT
    schema_name,
    table_name,
    index_name,
    index_size_bytes,
    (expected_pages * bs)::bigint AS expected_size_bytes,
    GREATEST(0, (index_size_bytes - (expected_pages * bs)::bigint)) AS bloat_bytes,
    ROUND(
        (
            GREATEST(0, (index_size_bytes - (expected_pages * bs)::bigint))::numeric
            / NULLIF(index_size_bytes, 0)
            * 100
        )::numeric,
        2
    )::float AS bloat_ratio_pct
FROM index_est
ORDER BY bloat_bytes DESC;
"""

SQL_INDEX_BLOAT_SCHEMAS_PSYCOPG = """
WITH constants AS (
    SELECT
        current_setting('block_size')::numeric AS bs,
        40 AS page_hdr,
        8 AS tpl_hdr,
        8 AS ma
),
index_stats AS (
    SELECT
        n.nspname AS schema_name,
        c.relname AS table_name,
        i.relname AS index_name,
        i.relpages,
        i.reltuples,
        COALESCE(
            SUM(
                (1.0 - COALESCE(s.null_frac, 0.0)) * COALESCE(s.avg_width, 8)
            ),
            16
        ) AS data_width
    FROM pg_class i
    JOIN pg_index x ON x.indexrelid = i.oid
    JOIN pg_class c ON c.oid = x.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_am am ON am.oid = i.relam AND am.amname = 'btree'
    LEFT JOIN pg_stats s ON s.schemaname = n.nspname
                        AND s.tablename = c.relname
                        AND s.attname = ANY(
                            SELECT a.attname
                            FROM pg_attribute a
                            WHERE a.attrelid = c.oid
                              AND a.attnum = ANY(string_to_array(x.indkey::text, ' ')::int[])
                        )
    WHERE n.nspname = ANY(%s)
      AND i.relpages > 0
    GROUP BY n.nspname, c.relname, i.relname, i.relpages, i.reltuples
),
index_est AS (
    SELECT
        is_s.schema_name,
        is_s.table_name,
        is_s.index_name,
        is_s.relpages,
        is_s.reltuples,
        k.bs,
        (k.bs * is_s.relpages)::bigint AS index_size_bytes,
        CEIL(
            (is_s.reltuples * (
                CEIL((k.tpl_hdr + is_s.data_width)::numeric / k.ma) * k.ma + 4
            )) / NULLIF((k.bs - k.page_hdr) * 0.70, 0)
        )::bigint AS expected_pages
    FROM index_stats is_s
    CROSS JOIN constants k
)
SELECT
    schema_name,
    table_name,
    index_name,
    index_size_bytes,
    (expected_pages * bs)::bigint AS expected_size_bytes,
    GREATEST(0, (index_size_bytes - (expected_pages * bs)::bigint)) AS bloat_bytes,
    ROUND(
        (
            GREATEST(0, (index_size_bytes - (expected_pages * bs)::bigint))::numeric
            / NULLIF(index_size_bytes, 0)
            * 100
        )::numeric,
        2
    )::float AS bloat_ratio_pct
FROM index_est
ORDER BY bloat_bytes DESC;
"""

SQL_INDEX_BLOAT_ASYNCPG = """
WITH constants AS (
    SELECT
        current_setting('block_size')::numeric AS bs,
        40 AS page_hdr,
        8 AS tpl_hdr,
        8 AS ma
),
index_stats AS (
    SELECT
        n.nspname AS schema_name,
        c.relname AS table_name,
        i.relname AS index_name,
        i.relpages,
        i.reltuples,
        COALESCE(
            SUM(
                (1.0 - COALESCE(s.null_frac, 0.0)) * COALESCE(s.avg_width, 8)
            ),
            16
        ) AS data_width
    FROM pg_class i
    JOIN pg_index x ON x.indexrelid = i.oid
    JOIN pg_class c ON c.oid = x.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_am am ON am.oid = i.relam AND am.amname = 'btree'
    LEFT JOIN pg_stats s ON s.schemaname = n.nspname
                        AND s.tablename = c.relname
                        AND s.attname = ANY(
                            SELECT a.attname
                            FROM pg_attribute a
                            WHERE a.attrelid = c.oid
                              AND a.attnum = ANY(string_to_array(x.indkey::text, ' ')::int[])
                        )
    WHERE n.nspname = ANY($1)
      AND i.relpages > 0
    GROUP BY n.nspname, c.relname, i.relname, i.relpages, i.reltuples
),
index_est AS (
    SELECT
        is_s.schema_name,
        is_s.table_name,
        is_s.index_name,
        is_s.relpages,
        is_s.reltuples,
        k.bs,
        (k.bs * is_s.relpages)::bigint AS index_size_bytes,
        CEIL(
            (is_s.reltuples * (
                CEIL((k.tpl_hdr + is_s.data_width)::numeric / k.ma) * k.ma + 4
            )) / NULLIF((k.bs - k.page_hdr) * 0.70, 0)
        )::bigint AS expected_pages
    FROM index_stats is_s
    CROSS JOIN constants k
)
SELECT
    schema_name,
    table_name,
    index_name,
    index_size_bytes,
    (expected_pages * bs)::bigint AS expected_size_bytes,
    GREATEST(0, (index_size_bytes - (expected_pages * bs)::bigint)) AS bloat_bytes,
    ROUND(
        (
            GREATEST(0, (index_size_bytes - (expected_pages * bs)::bigint))::numeric
            / NULLIF(index_size_bytes, 0)
            * 100
        )::numeric,
        2
    )::float AS bloat_ratio_pct
FROM index_est
ORDER BY bloat_bytes DESC;
"""
