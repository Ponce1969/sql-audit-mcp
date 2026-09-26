"""Tests for the canonical contract (AuditReport, Finding, Evidence) across CLI and MCP."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import audit_pg
from audit_pg import (
    DatabaseHealthReport,
)
from audit_pg import (
    DeadTuplesIssue as CliDeadTuples,
)
from audit_pg import (
    HotUpdateIssue as CliHot,
)
from audit_pg import (
    InvalidIndexIssue as CliInvalid,
)
from audit_pg import (
    LowUsageIndexIssue as CliLowUsage,
)
from audit_pg import (
    RedundantIndexIssue as CliRedundant,
)
from audit_pg import (
    UnindexedFKIssue as CliUnindexedFK,
)
from mcp_pg_auditor import (
    DeadTuplesIssue as McpDeadTuples,
)
from mcp_pg_auditor import (
    HotUpdateIssue as McpHot,
)
from mcp_pg_auditor import (
    InvalidIndexIssue as McpInvalid,
)
from mcp_pg_auditor import (
    LowUsageIndexIssue as McpLowUsage,
)
from mcp_pg_auditor import (
    PostgresHealthReport,
)
from mcp_pg_auditor import (
    RedundantIndexIssue as McpRedundant,
)
from mcp_pg_auditor import (
    UnindexedFKIssue as McpUnindexedFK,
)


def test_cli_and_mcp_produce_equivalent_audit_reports():
    """Demonstrates equivalence between CLI and MCP report translation to AuditReport."""
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Build CLI report
    cli_report = DatabaseHealthReport(
        hot_issues=[CliHot("users", 1000, 150, 15.0, 100, True, 25000, "18 MB")],
        redundant_indexes=[
            CliRedundant(
                "users",
                "idx_users_email",
                "16 MB",
                "idx_users_email_created",
                "def a",
                "def b",
            )
        ],
        low_usage_indexes=[
            CliLowUsage("orders", "idx_orders_status", "8 MB", 5, 2000, 0.0025, 50000, "45 MB")
        ],
        invalid_indexes=[CliInvalid("orders", "idx_orders_broken", "24 MB")],
        unindexed_fks=[CliUnindexedFK("order_items", "fk_items_prod", "products", "def fk")],
        autovacuum_dead_tuples=[CliDeadTuples("events", 15000, 50000, 23.08, now, None)],
    )

    # 2. Build equivalent MCP report
    mcp_report = PostgresHealthReport(
        database_alias="test_db",
        schemas_audited=["public"],
        execution_time_ms=12.5,
        invalid_indexes=[
            McpInvalid(
                child_table="orders",
                invalid_index="idx_orders_broken",
                index_size="24 MB",
            )
        ],
        unindexed_fks=[
            McpUnindexedFK(
                child_table="order_items",
                fk_name="fk_items_prod",
                parent_table="products",
                fk_definition="def fk",
            )
        ],
        autovacuum_dead_tuples=[
            McpDeadTuples(
                table_name="events",
                dead_tuples=15000,
                live_tuples=50000,
                dead_tuple_pct=23.08,
                last_autovacuum=now,
                last_vacuum=None,
            )
        ],
        hot_fillfactor_issues=[
            McpHot(
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
            McpRedundant(
                table_name="users",
                redundant_index="idx_users_email",
                redundant_size="16 MB",
                covering_index="idx_users_email_created",
                redundant_def="def a",
                covering_def="def b",
            )
        ],
        low_usage_indexes=[
            McpLowUsage(
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
    )

    cli_canonical = cli_report.to_audit_report(
        database="test_db",
        checks=["hot", "redundant", "low-usage", "invalid", "unindexed-fks", "dead-tuples"],
        observed_at=now,
        audit_id="audit_fixed_id",
    )

    mcp_canonical = mcp_report.to_audit_report(
        checks=["hot", "redundant", "low-usage", "invalid", "unindexed-fks", "dead-tuples"],
        observed_at=now,
        audit_id="audit_fixed_id",
    )

    # Validate findings count
    assert len(cli_canonical.findings) == 6
    assert len(mcp_canonical.findings) == 6

    # Validate finding_ids and evidence_ids match exactly between both adapters
    cli_findings = {f.finding_id: f for f in cli_canonical.findings}
    mcp_findings = {f.finding_id: f for f in mcp_canonical.findings}

    assert set(cli_findings.keys()) == set(mcp_findings.keys())

    for fid in cli_findings:
        cf = cli_findings[fid]
        mf = mcp_findings[fid]
        assert cf.severity == mf.severity
        assert cf.object_name == mf.object_name
        assert cf.evidence[0].evidence_id == mf.evidence[0].evidence_id


def test_cli_canonical_json_flag(capsys):
    """--canonical-json CLI flag emits valid AuditReport schema."""
    report = DatabaseHealthReport(
        invalid_indexes=[CliInvalid("orders", "idx_orders_broken", "12 MB")]
    )

    def factory(db_url, connect_timeout=10):
        mock = MagicMock()
        mock.run_audit.return_value = report
        return mock

    exit_code = audit_pg.main(
        ["--url", "postgresql://u:p@localhost:5432/orders_db", "--canonical-json"],
        auditor_factory=factory,
    )
    assert exit_code == 3  # Critical issue detected
    captured = capsys.readouterr().out
    data = json.loads(captured)

    assert data["schema_version"] == "1.0.0"
    assert data["database"] == "localhost"
    assert data["has_critical_issues"] is True
    assert len(data["findings"]) == 1
    finding = data["findings"][0]
    assert finding["finding_id"] == "PG-INDEX-INVALID:orders:idx_orders_broken"
    assert finding["severity"] == "critical"
    assert finding["evidence"][0]["evidence_id"].startswith("sha256:")
