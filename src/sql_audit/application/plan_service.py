"""Application service for PostgreSQL query plan analysis and impact simulation."""

from datetime import datetime, timezone
from typing import Any

from sql_audit.domain.plan import (
    PlanAnalysisReport,
    PlanRiskLevel,
    PlanSummary,
    PlanWarning,
)


def analyze_execution_plan(
    raw_plan_data: list[dict[str, Any]] | dict[str, Any],
    query: str = "",
    database: str = "localhost",
    observed_at: datetime | None = None,
) -> PlanAnalysisReport:
    """Parses and audits a PostgreSQL JSON EXPLAIN or EXPLAIN ANALYZE structure."""
    obs_time = observed_at or datetime.now(timezone.utc)

    if isinstance(raw_plan_data, list):
        plan_root = raw_plan_data[0] if raw_plan_data else {}
    else:
        plan_root = raw_plan_data

    plan_node = plan_root.get("Plan", {})
    planning_time = plan_root.get("Planning Time")
    execution_time = plan_root.get("Execution Time")
    is_analyzed = execution_time is not None

    total_cost = float(plan_node.get("Total Cost", 0.0))

    shared_hit = 0
    shared_read = 0
    temp_written = 0
    has_seq_scan_large = False
    has_disk_sort = False
    has_high_skew = False

    warnings: list[PlanWarning] = []

    def _traverse_node(node: dict[str, Any]) -> None:
        nonlocal shared_hit, shared_read, temp_written
        nonlocal has_seq_scan_large, has_disk_sort, has_high_skew

        node_type = str(node.get("Node Type", "Unknown"))
        relation_name = str(node.get("Relation Name", ""))
        plan_rows = int(node.get("Plan Rows", 0))
        actual_rows = node.get("Actual Rows")
        node_cost = float(node.get("Total Cost", 0.0))

        # Accumulate buffer metrics
        shared_hit += int(node.get("Shared Hit Blocks", 0))
        shared_read += int(node.get("Shared Read Blocks", 0))
        node_temp_written = int(node.get("Temp Written Blocks", 0))
        temp_written += node_temp_written

        # 1. Sequential scan on large relation
        if node_type == "Seq Scan":
            eval_rows = int(actual_rows) if actual_rows is not None else plan_rows
            if eval_rows >= 10_000:
                has_seq_scan_large = True
                risk = (
                    PlanRiskLevel.HIGH
                    if eval_rows >= 50_000 or node_cost >= 10_000.0
                    else PlanRiskLevel.MEDIUM
                )
                warnings.append(
                    PlanWarning(
                        warning_type="seq_scan_large_table",
                        risk_level=risk,
                        node_type=node_type,
                        relation_name=relation_name,
                        message=(
                            f"Sequential scan on table '{relation_name}' with {eval_rows:,} "
                            f"rows (cost: {node_cost:.1f}). Check for missing indexes on filter "
                            f"predicates."
                        ),
                    )
                )

        # 2. Disk spill (WorkMem)
        sort_method = str(node.get("Sort Method", ""))
        is_spill = (
            "disk" in sort_method.lower()
            or "external" in sort_method.lower()
            or node_temp_written > 0
        )
        if is_spill:
            has_disk_sort = True
            spill_detail = sort_method or f"{node_temp_written} temp blocks"
            warnings.append(
                PlanWarning(
                    warning_type="workmem_disk_spill",
                    risk_level=PlanRiskLevel.HIGH,
                    node_type=node_type,
                    relation_name=relation_name,
                    message=(
                        f"Node '{node_type}' spilled to disk ({spill_detail}). "
                        f"Query memory exceeded work_mem allocation."
                    ),
                )
            )

        # 3. Estimation skew
        if actual_rows is not None and plan_rows > 0:
            act = float(actual_rows)
            pln = float(plan_rows)
            if act >= 100:
                ratio = act / pln
                if ratio >= 10.0:
                    has_high_skew = True
                    risk = (
                        PlanRiskLevel.HIGH
                        if act >= 10_000 and ratio >= 50.0
                        else PlanRiskLevel.MEDIUM
                    )
                    warnings.append(
                        PlanWarning(
                            warning_type="estimation_skew_underestimated",
                            risk_level=risk,
                            node_type=node_type,
                            relation_name=relation_name,
                            message=(
                                f"Planner underestimation on '{node_type}' ({relation_name}): "
                                f"estimated {plan_rows:,} rows vs actual {int(act):,} rows "
                                f"({ratio:.1f}x skew). Statistics may be stale."
                            ),
                        )
                    )
                elif ratio <= 0.1:
                    has_high_skew = True
                    warnings.append(
                        PlanWarning(
                            warning_type="estimation_skew_overestimated",
                            risk_level=PlanRiskLevel.MEDIUM,
                            node_type=node_type,
                            relation_name=relation_name,
                            message=(
                                f"Planner overestimation on '{node_type}' ({relation_name}): "
                                f"estimated {plan_rows:,} rows vs actual {int(act):,} rows "
                                f"({ratio:.2f}x skew). May cause inefficient join orders."
                            ),
                        )
                    )

        # 4. Nested loop with high iterations
        if node_type == "Nested Loop" and is_analyzed:
            actual_loops = int(node.get("Actual Loops", 1))
            actual_total_time = float(node.get("Actual Total Time", 0.0))
            if actual_loops >= 10_000 or actual_total_time >= 1000.0:
                warnings.append(
                    PlanWarning(
                        warning_type="nested_loop_high_loops",
                        risk_level=PlanRiskLevel.HIGH,
                        node_type=node_type,
                        relation_name=relation_name,
                        message=(
                            f"Nested loop executed {actual_loops:,} iterations taking "
                            f"{actual_total_time:.1f} ms. Verify index on join condition."
                        ),
                    )
                )

        # Traverse child plans
        for sub_plan in node.get("Plans", []):
            _traverse_node(sub_plan)

    if plan_node:
        _traverse_node(plan_node)

    # 5. Buffer cache miss check
    total_blocks = shared_hit + shared_read
    if total_blocks > 0 and shared_read >= 2000:
        hit_ratio = (shared_hit / total_blocks) * 100.0
        if hit_ratio < 80.0:
            warnings.append(
                PlanWarning(
                    warning_type="low_cache_hit_ratio",
                    risk_level=PlanRiskLevel.MEDIUM,
                    node_type="Summary",
                    relation_name="",
                    message=(
                        f"Heavy disk reads: {shared_read:,} blocks read from disk with only "
                        f"{hit_ratio:.1f}% cache hit ratio."
                    ),
                )
            )

    # Sort warnings by risk level (CRITICAL -> HIGH -> MEDIUM -> LOW -> INFO)
    risk_order = {
        PlanRiskLevel.CRITICAL: 0,
        PlanRiskLevel.HIGH: 1,
        PlanRiskLevel.MEDIUM: 2,
        PlanRiskLevel.LOW: 3,
        PlanRiskLevel.INFO: 4,
    }
    warnings.sort(key=lambda w: risk_order.get(w.risk_level, 99))

    summary = PlanSummary(
        total_cost=total_cost,
        planning_time_ms=float(planning_time) if planning_time is not None else None,
        execution_time_ms=float(execution_time) if execution_time is not None else None,
        shared_hit_blocks=shared_hit,
        shared_read_blocks=shared_read,
        temp_written_blocks=temp_written,
        has_seq_scan_large_table=has_seq_scan_large,
        has_disk_sort=has_disk_sort,
        has_high_estimation_skew=has_high_skew,
        warnings=warnings,
    )

    return PlanAnalysisReport(
        observed_at=obs_time,
        query=query.strip(),
        is_analyzed=is_analyzed,
        database=database,
        summary=summary,
        raw_plan=plan_root,
    )


def render_plan_report_text(report: PlanAnalysisReport) -> str:
    """Renders human-readable textual summary of the execution plan analysis."""
    lines = [
        "=" * 64,
        f"POSTGRESQL EXECUTION PLAN AUDIT: {report.database}",
        f"Mode: {'EXPLAIN ANALYZE (Executed)' if report.is_analyzed else 'EXPLAIN (Estimated)'}",
    ]

    s = report.summary
    if s.execution_time_ms is not None:
        lines.append(f"Execution Time: {s.execution_time_ms:.2f} ms")
    if s.planning_time_ms is not None:
        lines.append(f"Planning Time:  {s.planning_time_ms:.2f} ms")
    lines.append(f"Total Cost:     {s.total_cost:.2f}")

    total_blocks = s.shared_hit_blocks + s.shared_read_blocks
    if total_blocks > 0:
        hit_pct = (s.shared_hit_blocks / total_blocks) * 100.0
        lines.append(
            f"Buffers:        {s.shared_hit_blocks:,} hit, {s.shared_read_blocks:,} read "
            f"({hit_pct:.1f}% hit ratio)"
        )
    if s.temp_written_blocks > 0:
        lines.append(f"Temp Written:   {s.temp_written_blocks:,} blocks (spilled to disk)")

    lines.append("=" * 64)

    if not s.warnings:
        lines.append("\nPLAN HEALTH: No critical anomalies or bottlenecks detected.")
    else:
        lines.append(f"\nPLAN WARNINGS ({len(s.warnings)}):")
        for idx, w in enumerate(s.warnings, 1):
            lines.append(f"\n[{idx}] [{w.risk_level.value.upper()}] {w.warning_type}")
            target_str = f" on {w.relation_name}" if w.relation_name else ""
            lines.append(f"    Node:    {w.node_type}{target_str}")
            lines.append(f"    Message: {w.message}")

    lines.append("=" * 64)
    return "\n".join(lines)


def validate_explain_query(query: str) -> str:
    """Validates that a query string is suitable for passive EXPLAIN analysis.

    Enforces that the query is non-empty, strips a single trailing semicolon,
    and rejects multi-statement SQL strings containing internal semicolons.
    """
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("Query string cannot be empty.")

    cleaned = cleaned.rstrip(";").strip()
    if not cleaned:
        raise ValueError("Query string cannot be empty.")

    in_single_quote = False
    in_double_quote = False
    idx = 0
    length = len(cleaned)

    while idx < length:
        ch = cleaned[idx]
        if ch == "'" and not in_double_quote:
            if in_single_quote and idx + 1 < length and cleaned[idx + 1] == "'":
                idx += 2
                continue
            in_single_quote = not in_single_quote
        elif ch == '"' and not in_single_quote:
            if in_double_quote and idx + 1 < length and cleaned[idx + 1] == '"':
                idx += 2
                continue
            in_double_quote = not in_double_quote
        elif ch == ";" and not in_single_quote and not in_double_quote:
            raise ValueError("Multi-statement queries are not allowed in explain_query.")
        idx += 1

    return cleaned


__all__ = [
    "analyze_execution_plan",
    "render_plan_report_text",
    "validate_explain_query",
]
