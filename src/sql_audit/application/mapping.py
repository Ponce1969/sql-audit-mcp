"""Deterministic mapping from raw database evidence to canonical domain models."""

import uuid
from datetime import datetime, timezone
from typing import Any

from sql_audit.domain.hasher import compute_evidence_id, compute_stable_finding_id
from sql_audit.domain.models import (
    AuditReport,
    Evidence,
    ExecutionMetadata,
    Finding,
    JsonValue,
    Severity,
)


def map_invalid_index_to_finding(
    row: dict[str, Any],
    observed_at: datetime,
    server_version: str = "unknown",
) -> Finding:
    child_table = str(row.get("child_table", ""))
    invalid_index = str(row.get("invalid_index", ""))
    index_size = str(row.get("index_size", ""))

    values: dict[str, JsonValue] = {
        "child_table": child_table,
        "invalid_index": invalid_index,
        "index_size": index_size,
    }
    evidence_id = compute_evidence_id("pg_catalog", "invalid_indexes", values)
    evidence = Evidence(
        source="pg_catalog",
        observed_at=observed_at,
        server_version=server_version,
        query_name="invalid_indexes",
        values=values,
        evidence_id=evidence_id,
    )
    return Finding(
        finding_id=compute_stable_finding_id(
            "PG-INDEX-INVALID", "table", child_table, invalid_index
        ),
        check="invalid_indexes",
        severity=Severity.CRITICAL,
        object_type="index",
        object_name=f"{child_table}.{invalid_index}",
        reason="Aborted concurrent index build left unusable index structure paying write penalty",
        evidence=[evidence],
    )


def map_unindexed_fk_to_finding(
    row: dict[str, Any],
    observed_at: datetime,
    server_version: str = "unknown",
) -> Finding:
    child_table = str(row.get("child_table", ""))
    fk_name = str(row.get("fk_name", ""))
    parent_table = str(row.get("parent_table", ""))
    fk_definition = str(row.get("fk_definition", ""))

    values: dict[str, JsonValue] = {
        "child_table": child_table,
        "fk_name": fk_name,
        "parent_table": parent_table,
        "fk_definition": fk_definition,
    }
    evidence_id = compute_evidence_id("pg_catalog", "unindexed_fks", values)
    evidence = Evidence(
        source="pg_catalog",
        observed_at=observed_at,
        server_version=server_version,
        query_name="unindexed_fks",
        values=values,
        evidence_id=evidence_id,
    )
    return Finding(
        finding_id=compute_stable_finding_id("PG-FK-UNINDEXED", "table", child_table, fk_name),
        check="unindexed_fks",
        severity=Severity.HIGH,
        object_type="constraint",
        object_name=f"{child_table}.{fk_name}",
        reason="Missing supporting left-prefix B-Tree index on foreign key referencing columns",
        evidence=[evidence],
    )


def map_dead_tuples_to_finding(
    row: dict[str, Any],
    observed_at: datetime,
    server_version: str = "unknown",
) -> Finding:
    table_name = str(row.get("table_name", ""))
    dead_tuples = int(row.get("dead_tuples", 0))
    live_tuples = int(row.get("live_tuples", 0))
    dead_tuple_pct = float(row.get("dead_tuple_pct", 0.0))
    last_autovacuum = row.get("last_autovacuum")
    last_vacuum = row.get("last_vacuum")

    last_auto_iso = last_autovacuum.isoformat() if isinstance(last_autovacuum, datetime) else None
    last_vac_iso = last_vacuum.isoformat() if isinstance(last_vacuum, datetime) else None

    values: dict[str, JsonValue] = {
        "table_name": table_name,
        "dead_tuples": dead_tuples,
        "live_tuples": live_tuples,
        "dead_tuple_pct": dead_tuple_pct,
        "last_autovacuum": last_auto_iso,
        "last_vacuum": last_vac_iso,
    }
    evidence_id = compute_evidence_id("pg_stat", "autovacuum_dead_tuples", values)
    evidence = Evidence(
        source="pg_stat",
        observed_at=observed_at,
        server_version=server_version,
        query_name="autovacuum_dead_tuples",
        values=values,
        evidence_id=evidence_id,
    )
    return Finding(
        finding_id=compute_stable_finding_id("PG-VACUUM-DEAD-TUPLES", "table", table_name),
        check="autovacuum_dead_tuples",
        severity=Severity.HIGH,
        object_type="table",
        object_name=table_name,
        reason=(
            "Dead tuple accumulation exceeding 10,000 tuples and 15% threshold "
            "indicates autovacuum lag"
        ),
        evidence=[evidence],
    )


def map_hot_to_finding(
    row: dict[str, Any],
    observed_at: datetime,
    server_version: str = "unknown",
) -> Finding:
    table_name = str(row.get("table_name", ""))
    total_updates = int(row.get("total_updates", 0))
    hot_updates = int(row.get("hot_updates", 0))
    hot_ratio_pct = float(row.get("hot_ratio_pct", 0.0))
    fillfactor = int(row.get("fillfactor", 100))
    fillfactor_warning = bool(row.get("fillfactor_warning", fillfactor == 100))
    table_rows = int(row.get("table_rows", 0))
    table_size = str(row.get("table_size", ""))

    values: dict[str, JsonValue] = {
        "table_name": table_name,
        "total_updates": total_updates,
        "hot_updates": hot_updates,
        "hot_ratio_pct": hot_ratio_pct,
        "fillfactor": fillfactor,
        "fillfactor_warning": fillfactor_warning,
        "table_rows": table_rows,
        "table_size": table_size,
    }
    evidence_id = compute_evidence_id("pg_stat", "hot_and_fillfactor", values)
    evidence = Evidence(
        source="pg_stat",
        observed_at=observed_at,
        server_version=server_version,
        query_name="hot_and_fillfactor",
        values=values,
        evidence_id=evidence_id,
    )
    return Finding(
        finding_id=compute_stable_finding_id("PG-HOT-FILLFACTOR", "table", table_name),
        check="hot_fillfactor",
        severity=Severity.MEDIUM,
        object_type="table",
        object_name=table_name,
        reason=(
            "Low Heap-Only Tuple (HOT) update ratio with default fillfactor prevents "
            "in-page updates"
        ),
        evidence=[evidence],
    )


def map_redundant_index_to_finding(
    row: dict[str, Any],
    observed_at: datetime,
    server_version: str = "unknown",
) -> Finding:
    table_name = str(row.get("table_name", ""))
    redundant_index = str(row.get("redundant_index", ""))
    redundant_size = str(row.get("redundant_size", ""))
    covering_index = str(row.get("covering_index", ""))
    redundant_def = str(row.get("redundant_def", ""))
    covering_def = str(row.get("covering_def", ""))

    values: dict[str, JsonValue] = {
        "table_name": table_name,
        "redundant_index": redundant_index,
        "redundant_size": redundant_size,
        "covering_index": covering_index,
        "redundant_def": redundant_def,
        "covering_def": covering_def,
    }
    evidence_id = compute_evidence_id("pg_catalog", "redundant_indexes", values)
    evidence = Evidence(
        source="pg_catalog",
        observed_at=observed_at,
        server_version=server_version,
        query_name="redundant_indexes",
        values=values,
        evidence_id=evidence_id,
    )
    return Finding(
        finding_id=compute_stable_finding_id(
            "PG-INDEX-REDUNDANT", "table", table_name, redundant_index
        ),
        check="redundant_indexes",
        severity=Severity.LOW,
        object_type="index",
        object_name=f"{table_name}.{redundant_index}",
        reason="B-Tree index is redundant with a wider covering index sharing left-prefix columns",
        evidence=[evidence],
    )


def map_low_usage_index_to_finding(
    row: dict[str, Any],
    observed_at: datetime,
    server_version: str = "unknown",
) -> Finding:
    table_name = str(row.get("table_name", ""))
    index_name = str(row.get("index_name", ""))
    size = str(row.get("size", ""))
    index_scans = int(row.get("index_scans", 0))
    table_writes = int(row.get("table_writes", 0))
    read_write_ratio = float(row.get("read_write_ratio", 0.0))
    table_rows = int(row.get("table_rows", 0))
    table_size = str(row.get("table_size", ""))

    values: dict[str, JsonValue] = {
        "table_name": table_name,
        "index_name": index_name,
        "size": size,
        "index_scans": index_scans,
        "table_writes": table_writes,
        "read_write_ratio": read_write_ratio,
        "table_rows": table_rows,
        "table_size": table_size,
    }
    evidence_id = compute_evidence_id("pg_stat", "low_usage_indexes", values)
    evidence = Evidence(
        source="pg_stat",
        observed_at=observed_at,
        server_version=server_version,
        query_name="low_usage_indexes",
        values=values,
        evidence_id=evidence_id,
    )
    return Finding(
        finding_id=compute_stable_finding_id("PG-INDEX-LOW-USAGE", "table", table_name, index_name),
        check="low_usage_indexes",
        severity=Severity.LOW,
        object_type="index",
        object_name=f"{table_name}.{index_name}",
        reason="Index has high write maintenance overhead and negligible read scans",
        evidence=[evidence],
    )


def build_audit_report(
    database: str,
    checks_executed: list[str],
    findings: list[Finding],
    errors: list[str] | None = None,
    observed_at: datetime | None = None,
    duration_ms: float = 0.0,
    schemas: list[str] | None = None,
    min_size_bytes: int = 0,
    min_table_rows: int = 10000,
    server_version: str = "unknown",
    audit_id: str | None = None,
) -> AuditReport:
    """Builds a canonical AuditReport root contract."""
    obs_time = observed_at or datetime.now(timezone.utc)
    aid = audit_id or f"audit_{uuid.uuid4().hex[:12]}"
    has_critical = any(f.severity == Severity.CRITICAL for f in findings)

    summary: dict[str, int] = {}
    for f in findings:
        summary[f.check] = summary.get(f.check, 0) + 1

    return AuditReport(
        schema_version="1.0.0",
        audit_id=aid,
        database=database,
        server_version=server_version,
        observed_at=obs_time,
        execution_metadata=ExecutionMetadata(
            duration_ms=duration_ms,
            schemas=schemas,
            min_size_bytes=min_size_bytes,
            min_table_rows=min_table_rows,
        ),
        checks_executed=list(checks_executed),
        has_critical_issues=has_critical,
        summary=summary,
        findings=findings,
        errors=errors or [],
    )


def _extract_row(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return dict(item.model_dump())
    return dict(item.__dict__)


def convert_legacy_report_to_audit_report(
    report: Any,
    database: str,
    checks: list[str],
    observed_at: datetime | None = None,
    server_version: str = "unknown",
    duration_ms: float = 0.0,
    schemas: list[str] | None = None,
    min_size_bytes: int = 0,
    min_table_rows: int = 10000,
    audit_id: str | None = None,
) -> AuditReport:
    """Translates a legacy DatabaseHealthReport or PostgresHealthReport into an AuditReport."""
    obs_time = observed_at or datetime.now(timezone.utc)
    findings: list[Finding] = []

    # 1. Invalid indexes
    for item in getattr(report, "invalid_indexes", []):
        row = _extract_row(item)
        findings.append(map_invalid_index_to_finding(row, obs_time, server_version))

    # 2. Unindexed foreign keys
    for item in getattr(report, "unindexed_fks", []):
        row = _extract_row(item)
        findings.append(map_unindexed_fk_to_finding(row, obs_time, server_version))

    # 3. Dead tuples
    for item in getattr(report, "autovacuum_dead_tuples", []):
        row = _extract_row(item)
        findings.append(map_dead_tuples_to_finding(row, obs_time, server_version))

    # 4. HOT issues (could be named hot_issues or hot_fillfactor_issues)
    hot_list = getattr(report, "hot_issues", None) or getattr(report, "hot_fillfactor_issues", [])
    for item in hot_list:
        row = _extract_row(item)
        findings.append(map_hot_to_finding(row, obs_time, server_version))

    # 5. Redundant indexes
    for item in getattr(report, "redundant_indexes", []):
        row = _extract_row(item)
        findings.append(map_redundant_index_to_finding(row, obs_time, server_version))

    # 6. Low usage indexes
    for item in getattr(report, "low_usage_indexes", []):
        row = _extract_row(item)
        findings.append(map_low_usage_index_to_finding(row, obs_time, server_version))

    return build_audit_report(
        database=database,
        checks_executed=checks,
        findings=findings,
        observed_at=obs_time,
        duration_ms=duration_ms,
        schemas=schemas,
        min_size_bytes=min_size_bytes,
        min_table_rows=min_table_rows,
        server_version=server_version,
        audit_id=audit_id,
    )
