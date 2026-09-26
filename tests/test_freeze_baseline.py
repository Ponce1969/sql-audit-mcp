"""Baseline regression test for Phase 1.

Freezes current behavior and JSON output across all 6 audit checks.
Normalizes timestamps and dynamic execution values to prevent false positives.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from audit_pg import (
    ALL_CHECKS,
    DatabaseHealthReport,
    DeadTuplesIssue,
    HotUpdateIssue,
    InvalidIndexIssue,
    LowUsageIndexIssue,
    RedundantIndexIssue,
    UnindexedFKIssue,
    render_json,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "baseline_snapshot.json"


def make_canonical_sample_report() -> DatabaseHealthReport:
    """Creates a deterministic DatabaseHealthReport covering all 6 checks."""
    return DatabaseHealthReport(
        hot_issues=[
            HotUpdateIssue(
                table_name="users",
                total_updates=1000,
                hot_updates=150,
                hot_ratio_pct=15.0,
                fillfactor=100,
                fillfactor_warning=True,
                table_rows=25000,
                table_size="18 MB",
            )
        ],
        redundant_indexes=[
            RedundantIndexIssue(
                table_name="users",
                redundant_index="idx_users_email",
                redundant_size="16 MB",
                covering_index="idx_users_email_created",
                redundant_def="CREATE INDEX idx_users_email ON users(email)",
                covering_def="CREATE INDEX idx_users_email_created ON users(email, created_at)",
            )
        ],
        low_usage_indexes=[
            LowUsageIndexIssue(
                table_name="orders",
                index_name="idx_orders_status",
                size="8 MB",
                index_scans=5,
                table_writes=2000,
                read_write_ratio=0.0025,
                table_rows=50000,
                table_size="45 MB",
            )
        ],
        invalid_indexes=[
            InvalidIndexIssue(
                child_table="orders",
                invalid_index="idx_orders_broken",
                index_size="24 MB",
            )
        ],
        unindexed_fks=[
            UnindexedFKIssue(
                child_table="order_items",
                fk_name="fk_order_items_product",
                parent_table="products",
                fk_definition="FOREIGN KEY (product_id) REFERENCES products(id)",
            )
        ],
        autovacuum_dead_tuples=[
            DeadTuplesIssue(
                table_name="events",
                dead_tuples=15000,
                live_tuples=50000,
                dead_tuple_pct=23.08,
                last_autovacuum=datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc),
                last_vacuum=None,
            )
        ],
    )


def normalize_json_payload(payload: dict) -> dict:
    """Normalizes dynamic dates and timings for deterministic comparison."""
    normalized = dict(payload)
    # Recursively ensure stable structure
    return normalized


def test_baseline_json_matches_snapshot():
    """Validates that current render_json produces exact frozen baseline."""
    report = make_canonical_sample_report()
    current_json = render_json(report, database="prod_db.internal", checks=ALL_CHECKS)
    normalized_current = normalize_json_payload(current_json)

    assert FIXTURE_PATH.exists(), f"Missing fixture at {FIXTURE_PATH}"
    expected_json = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert normalized_current == expected_json
