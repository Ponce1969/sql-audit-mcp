# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "psycopg2-binary>=2.9.0",
#     "pydantic>=2.0.0",
#     "python-dotenv>=1.0.0",
# ]
# ///

import argparse
import os
import sys
from typing import Any
from urllib.parse import quote_plus

from dotenv import load_dotenv
from pydantic import BaseModel, Field

try:
    import psycopg2
    import psycopg2.extras

    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


class HotUpdateIssue(BaseModel):
    table_name: str
    total_updates: int
    hot_updates: int
    hot_ratio_pct: float
    fillfactor: int
    fillfactor_warning: bool


class RedundantIndexIssue(BaseModel):
    table_name: str
    redundant_index: str
    redundant_size: str
    covering_index: str
    redundant_def: str
    covering_def: str


class LowUsageIndexIssue(BaseModel):
    table_name: str
    index_name: str
    size: str
    index_scans: int
    table_writes: int
    read_write_ratio: float


class DatabaseHealthReport(BaseModel):
    hot_issues: list[HotUpdateIssue] = Field(default_factory=list)
    redundant_indexes: list[RedundantIndexIssue] = Field(default_factory=list)
    low_usage_indexes: list[LowUsageIndexIssue] = Field(default_factory=list)


class PostgresHealthAuditor:
    """Audits PostgreSQL catalog and runtime statistics for indexing and storage anti-patterns."""

    def __init__(self, db_url: str):
        self.db_url = db_url

    def run_audit(
        self,
        min_hot_ratio_pct: float = 30.0,
        min_updates_threshold: int = 50,
        max_rw_ratio: float = 0.05,
    ) -> DatabaseHealthReport:
        if not HAS_PSYCOPG2:
            raise RuntimeError("psycopg2 is not installed. Run: uv add psycopg2-binary")

        conn = psycopg2.connect(self.db_url)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                hot_issues = self._audit_hot_and_fillfactor(
                    cur, min_hot_ratio_pct, min_updates_threshold
                )
                redundant_indexes = self._audit_redundant_indexes(cur)
                low_usage = self._audit_low_usage_indexes(cur, max_rw_ratio)

                return DatabaseHealthReport(
                    hot_issues=hot_issues,
                    redundant_indexes=redundant_indexes,
                    low_usage_indexes=low_usage,
                )
        finally:
            conn.close()

    def _audit_hot_and_fillfactor(
        self, cur: Any, min_ratio: float, min_updates: int
    ) -> list[HotUpdateIssue]:
        query = """
        SELECT
            t.relname AS table_name,
            t.n_tup_upd AS total_updates,
            t.n_tup_hot_upd AS hot_updates,
            ROUND((t.n_tup_hot_upd::numeric / NULLIF(t.n_tup_upd, 0)) * 100, 2) AS hot_ratio_pct,
            COALESCE(
                (
                    SELECT split_part(opt, '=', 2)::int
                    FROM unnest(c.reloptions) AS opt
                    WHERE substr(opt, 1, 11) = 'fillfactor='
                    LIMIT 1
                ), 100
            ) AS fillfactor
        FROM pg_stat_user_tables t
        JOIN pg_class c ON c.oid = t.relid
        WHERE t.n_tup_upd >= %s
          AND ROUND((t.n_tup_hot_upd::numeric / NULLIF(t.n_tup_upd, 0)) * 100, 2) < %s
        ORDER BY t.n_tup_upd DESC;
        """
        cur.execute(query, (min_updates, min_ratio))
        rows = cur.fetchall()
        issues: list[HotUpdateIssue] = []
        for r in rows:
            data = dict(r)
            data["fillfactor_warning"] = data["fillfactor"] == 100
            issues.append(HotUpdateIssue(**data))
        return issues

    def _audit_redundant_indexes(self, cur: Any) -> list[RedundantIndexIssue]:
        # Detects exact duplicate indexes and left-prefix redundant indexes using CTE
        query = """
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
        ORDER BY p1.size_bytes DESC;
        """
        cur.execute(query)
        rows = cur.fetchall()
        return [RedundantIndexIssue(**dict(r)) for r in rows]

    def _audit_low_usage_indexes(self, cur: Any, max_ratio: float) -> list[LowUsageIndexIssue]:
        query = """
        SELECT
            i.relname AS table_name,
            i.indexrelname AS index_name,
            pg_size_pretty(pg_relation_size(i.indexrelid)) AS size,
            i.idx_scan AS index_scans,
            (t.n_tup_ins + t.n_tup_upd + t.n_tup_del) AS table_writes,
            ROUND(
                i.idx_scan::numeric / NULLIF(t.n_tup_ins + t.n_tup_upd + t.n_tup_del, 0), 4
            ) AS read_write_ratio
        FROM pg_stat_user_indexes i
        JOIN pg_stat_user_tables t ON i.relid = t.relid
        JOIN pg_index idx ON idx.indexrelid = i.indexrelid
        WHERE (t.n_tup_ins + t.n_tup_upd + t.n_tup_del) > 100
          AND NOT idx.indisprimary
          AND NOT idx.indisunique
          AND (i.idx_scan::numeric / NULLIF(t.n_tup_ins + t.n_tup_upd + t.n_tup_del, 0)) < %s
        ORDER BY pg_relation_size(i.indexrelid) DESC;
        """
        cur.execute(query, (max_ratio,))
        rows = cur.fetchall()
        return [LowUsageIndexIssue(**dict(r)) for r in rows]


def print_report(report: DatabaseHealthReport) -> None:
    print("\n" + "=" * 65)
    print("        POSTGRESQL STORAGE & INDEX HEALTH REPORT")
    print("=" * 65)

    # 1. Redundant Indexes
    print(f"\n[1] REDUNDANT / PREFIX INDEXES FOUND: {len(report.redundant_indexes)}")
    if not report.redundant_indexes:
        print("  -> OK: No redundant indexes detected.")
    else:
        for idx in report.redundant_indexes:
            print(f"  * Table: {idx.table_name}")
            print(f"    - Redundant: {idx.redundant_index} (Size: {idx.redundant_size})")
            print(f"    - Covered by: {idx.covering_index}")
            print(f"    - Redundant def: {idx.redundant_def}")
            print(f"    - Suggestion: Consider DROP INDEX {idx.redundant_index};\n")

    # 2. HOT Updates & Fillfactor
    print(f"[2] LOW HOT UPDATE EFFICIENCY: {len(report.hot_issues)}")
    if not report.hot_issues:
        print("  -> OK: No tables with low HOT update ratio.")
    else:
        for issue in report.hot_issues:
            warning = (
                " [!] WARNING: Default 100% fillfactor prevents HOT"
                if issue.fillfactor_warning
                else ""
            )
            print(f"  * Table: {issue.table_name}")
            print(
                f"    - HOT Ratio: {issue.hot_ratio_pct}% "
                f"({issue.hot_updates} HOT / {issue.total_updates} total updates)"
            )
            print(f"    - Fillfactor: {issue.fillfactor}%{warning}")
            if issue.fillfactor_warning:
                print(
                    f"    - Suggestion: ALTER TABLE {issue.table_name} "
                    f"SET (fillfactor = 85); VACUUM FULL {issue.table_name};\n"
                )

    # 3. Low Usage Indexes
    print(f"[3] UNPROFITABLE / HIGH-WRITE LOW-READ INDEXES: {len(report.low_usage_indexes)}")
    if not report.low_usage_indexes:
        print("  -> OK: No unprofitable indexes detected.")
    else:
        for low in report.low_usage_indexes:
            print(f"  * Table: {low.table_name}")
            print(f"    - Index: {low.index_name} (Size: {low.size})")
            print(
                f"    - Read Scans: {low.index_scans} | "
                f"Table Writes: {low.table_writes} (Ratio: {low.read_write_ratio})"
            )
            print("    - Suggestion: Evaluate if this index is required for queries.\n")

    print("=" * 65 + "\n")


def resolve_db_url(cli_url: str | None) -> str | None:
    if cli_url:
        return cli_url

    # Check current directory .env
    load_dotenv(os.path.join(os.getcwd(), ".env"))

    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return db_url

    # Fallback to separate POSTGRES_* environment variables
    if os.getenv("POSTGRES_DB"):
        user = quote_plus(os.getenv("POSTGRES_USER", "postgres"))
        pwd = quote_plus(os.getenv("POSTGRES_PASSWORD", ""))
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = os.getenv("POSTGRES_PORT", "5432")
        dbname = os.getenv("POSTGRES_DB")
        auth = f"{user}:{pwd}@" if user or pwd else ""
        return f"postgresql://{auth}{host}:{port}/{dbname}"

    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Centralized PostgreSQL Index and Storage Auditor")
    parser.add_argument(
        "--url",
        help="Postgres connection string. If omitted, reads .env from current directory",
    )
    parser.add_argument(
        "--min-hot-ratio",
        type=float,
        default=30.0,
        help="Minimum acceptable HOT ratio percentage (default: 30.0)",
    )
    parser.add_argument(
        "--min-updates",
        type=int,
        default=50,
        help="Minimum table updates to consider for HOT analysis (default: 50)",
    )
    parser.add_argument(
        "--max-rw-ratio",
        type=float,
        default=0.05,
        help="Maximum read/write ratio to flag an index as low-usage (default: 0.05)",
    )
    args = parser.parse_args()

    db_url = resolve_db_url(args.url)
    if not db_url:
        print("Error: DATABASE_URL or POSTGRES_DB not found in .env or arguments.", file=sys.stderr)
        sys.exit(1)

    auditor = PostgresHealthAuditor(db_url=db_url)
    try:
        report = auditor.run_audit(
            min_hot_ratio_pct=args.min_hot_ratio,
            min_updates_threshold=args.min_updates,
            max_rw_ratio=args.max_rw_ratio,
        )
        print_report(report)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to audit database: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
