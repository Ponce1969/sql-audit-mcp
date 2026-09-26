"""Application service for PostgreSQL physical bloat estimation."""

from datetime import datetime, timezone
from typing import Any

from sql_audit.domain.bloat import BloatReport, IndexBloat, TableBloat
from sql_audit.domain.hasher import compute_evidence_id, compute_stable_finding_id
from sql_audit.domain.models import Evidence, Finding, JsonValue, Severity


def _format_bytes(num_bytes: int) -> str:
    """Formats bytes into human-readable representation."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    num_float = float(num_bytes)
    for unit in ("kB", "MB", "GB", "TB"):
        num_float /= 1024.0
        if num_float < 1024.0:
            return f"{num_float:.1f} {unit}"
    return f"{num_float:.1f} PB"


def build_bloat_report(
    table_rows: list[dict[str, Any]],
    index_rows: list[dict[str, Any]],
    database: str = "localhost",
    min_bloat_bytes: int = 10_000_000,
    min_bloat_ratio_pct: float = 20.0,
    observed_at: datetime | None = None,
) -> BloatReport:
    """Builds a BloatReport from raw catalog estimation query rows."""
    obs_time = observed_at or datetime.now(timezone.utc)

    tables: list[TableBloat] = []
    for r in table_rows:
        b_bytes = int(r.get("bloat_bytes", 0))
        b_ratio = float(r.get("bloat_ratio_pct", 0.0))
        is_bloated = b_bytes >= min_bloat_bytes and b_ratio >= min_bloat_ratio_pct
        tables.append(
            TableBloat(
                schema_name=str(r.get("schema_name", "public")),
                table_name=str(r.get("table_name", "")),
                table_size_bytes=int(r.get("table_size_bytes", 0)),
                expected_size_bytes=int(r.get("expected_size_bytes", 0)),
                bloat_bytes=b_bytes,
                bloat_ratio_pct=b_ratio,
                is_bloated=is_bloated,
            )
        )

    indexes: list[IndexBloat] = []
    for r in index_rows:
        b_bytes = int(r.get("bloat_bytes", 0))
        b_ratio = float(r.get("bloat_ratio_pct", 0.0))
        is_bloated = b_bytes >= min_bloat_bytes and b_ratio >= min_bloat_ratio_pct
        indexes.append(
            IndexBloat(
                schema_name=str(r.get("schema_name", "public")),
                table_name=str(r.get("table_name", "")),
                index_name=str(r.get("index_name", "")),
                index_size_bytes=int(r.get("index_size_bytes", 0)),
                expected_size_bytes=int(r.get("expected_size_bytes", 0)),
                bloat_bytes=b_bytes,
                bloat_ratio_pct=b_ratio,
                is_bloated=is_bloated,
            )
        )

    # Sort descending by bloat_bytes
    tables.sort(key=lambda t: t.bloat_bytes, reverse=True)
    indexes.sort(key=lambda i: i.bloat_bytes, reverse=True)

    total_tbl_bloat = sum(t.bloat_bytes for t in tables if t.is_bloated)
    total_idx_bloat = sum(i.bloat_bytes for i in indexes if i.is_bloated)

    return BloatReport(
        observed_at=obs_time,
        database=database,
        min_bloat_bytes=min_bloat_bytes,
        min_bloat_ratio_pct=min_bloat_ratio_pct,
        tables=tables,
        indexes=indexes,
        total_table_bloat_bytes=total_tbl_bloat,
        total_index_bloat_bytes=total_idx_bloat,
    )


def bloat_report_to_findings(report: BloatReport, server_version: str = "16.0") -> list[Finding]:
    """Converts bloated tables and indexes into canonical Finding entities."""
    findings: list[Finding] = []

    for t in report.tables:
        if not t.is_bloated:
            continue

        if t.bloat_bytes >= 100_000_000 and t.bloat_ratio_pct >= 50.0:
            sev = Severity.CRITICAL
        elif t.bloat_bytes >= 50_000_000 or t.bloat_ratio_pct >= 40.0:
            sev = Severity.HIGH
        else:
            sev = Severity.MEDIUM

        values: dict[str, JsonValue] = {
            "schema_name": t.schema_name,
            "table_name": t.table_name,
            "table_size_bytes": t.table_size_bytes,
            "expected_size_bytes": t.expected_size_bytes,
            "bloat_bytes": t.bloat_bytes,
            "bloat_ratio_pct": t.bloat_ratio_pct,
            "table_size_pretty": _format_bytes(t.table_size_bytes),
            "bloat_pretty": _format_bytes(t.bloat_bytes),
        }
        eid = compute_evidence_id("pg_catalog", "table_bloat", values)
        evidence = Evidence(
            source="pg_catalog",
            observed_at=report.observed_at,
            server_version=server_version,
            query_name="table_bloat",
            values=values,
            evidence_id=eid,
        )
        finding_id = compute_stable_finding_id(
            "PG-BLOAT-TABLE", "table", f"{t.schema_name}.{t.table_name}"
        )
        reason = (
            f"Table {t.schema_name}.{t.table_name} has estimated {t.bloat_ratio_pct:.1f}% bloat "
            f"({_format_bytes(t.bloat_bytes)} wasted of {_format_bytes(t.table_size_bytes)})"
        )
        findings.append(
            Finding(
                finding_id=finding_id,
                check="table_bloat",
                severity=sev,
                object_type="table",
                object_name=f"{t.schema_name}.{t.table_name}",
                reason=reason,
                evidence=[evidence],
            )
        )

    for i in report.indexes:
        if not i.is_bloated:
            continue

        if i.bloat_bytes >= 50_000_000 and i.bloat_ratio_pct >= 50.0:
            sev = Severity.HIGH
        else:
            sev = Severity.MEDIUM

        idx_values: dict[str, JsonValue] = {
            "schema_name": i.schema_name,
            "table_name": i.table_name,
            "index_name": i.index_name,
            "index_size_bytes": i.index_size_bytes,
            "expected_size_bytes": i.expected_size_bytes,
            "bloat_bytes": i.bloat_bytes,
            "bloat_ratio_pct": i.bloat_ratio_pct,
            "index_size_pretty": _format_bytes(i.index_size_bytes),
            "bloat_pretty": _format_bytes(i.bloat_bytes),
        }
        eid = compute_evidence_id("pg_catalog", "index_bloat", idx_values)
        evidence = Evidence(
            source="pg_catalog",
            observed_at=report.observed_at,
            server_version=server_version,
            query_name="index_bloat",
            values=idx_values,
            evidence_id=eid,
        )
        finding_id = compute_stable_finding_id(
            "PG-BLOAT-INDEX", "index", f"{i.schema_name}.{i.index_name}"
        )
        reason = (
            f"B-tree index {i.schema_name}.{i.index_name} on {i.table_name} has estimated "
            f"{i.bloat_ratio_pct:.1f}% bloat ({_format_bytes(i.bloat_bytes)} wasted of "
            f"{_format_bytes(i.index_size_bytes)})"
        )
        findings.append(
            Finding(
                finding_id=finding_id,
                check="index_bloat",
                severity=sev,
                object_type="index",
                object_name=f"{i.schema_name}.{i.index_name}",
                reason=reason,
                evidence=[evidence],
            )
        )

    return findings


def render_bloat_report_text(report: BloatReport) -> str:
    """Formats bloat report as human-readable text."""
    bloated_tables = [t for t in report.tables if t.is_bloated]
    bloated_indexes = [i for i in report.indexes if i.is_bloated]

    if not bloated_tables and not bloated_indexes:
        return "ESTIMATED BLOAT: 0 bloated tables or indexes detected (clean)."

    lines = [
        "=" * 64,
        f"POSTGRESQL PHYSICAL BLOAT ESTIMATION: {report.database}",
        f"Total Table Bloat: {_format_bytes(report.total_table_bloat_bytes)}",
        f"Total Index Bloat: {_format_bytes(report.total_index_bloat_bytes)}",
        f"Thresholds: >= {_format_bytes(report.min_bloat_bytes)} and "
        f">= {report.min_bloat_ratio_pct:.1f}% bloat ratio",
        "=" * 64,
    ]

    if bloated_tables:
        lines.append(f"\n--- BLOATED TABLES ({len(bloated_tables)}) ---")
        for t in bloated_tables:
            lines.append(
                f"- {t.schema_name}.{t.table_name}: {_format_bytes(t.bloat_bytes)} bloat "
                f"({t.bloat_ratio_pct:.1f}%) | Real: {_format_bytes(t.table_size_bytes)} | "
                f"Expected: {_format_bytes(t.expected_size_bytes)}"
            )

    if bloated_indexes:
        lines.append(f"\n--- BLOATED B-TREE INDEXES ({len(bloated_indexes)}) ---")
        for idx in bloated_indexes:
            lines.append(
                f"- {idx.schema_name}.{idx.index_name} (on {idx.table_name}): "
                f"{_format_bytes(idx.bloat_bytes)} bloat ({idx.bloat_ratio_pct:.1f}%) | "
                f"Real: {_format_bytes(idx.index_size_bytes)} | "
                f"Expected: {_format_bytes(idx.expected_size_bytes)}"
            )

    lines.append("=" * 64)
    return "\n".join(lines)


__all__ = [
    "bloat_report_to_findings",
    "build_bloat_report",
    "render_bloat_report_text",
]
