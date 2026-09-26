"""Lock contention graph analysis and tree reconstruction service."""

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sql_audit.domain.hasher import compute_evidence_id, compute_stable_finding_id
from sql_audit.domain.locks import (
    BlockingEdge,
    LockContentionReport,
    LockProcess,
    LockTree,
)
from sql_audit.domain.models import Evidence, Finding, JsonValue, Severity


def _traverse_blocking_tree(
    parent_pid: int,
    blocking_to_blocked: dict[int, list[int]],
    processes_by_pid: dict[int, LockProcess],
    all_blocked: list[int],
    tree_lines: list[str],
    indent: str = "  ",
) -> float:
    """Recursively formats child blocked processes and returns max wait duration."""
    max_duration = 0.0
    children = blocking_to_blocked.get(parent_pid, [])
    for child_pid in children:
        all_blocked.append(child_pid)
        child_proc = processes_by_pid[child_pid]
        if child_proc.duration_sec > max_duration:
            max_duration = child_proc.duration_sec

        wait_desc = f"{child_proc.wait_event_type}:{child_proc.wait_event}".strip(":")
        wait_label = wait_desc or "lock"
        tree_lines.append(
            f"{indent}↓ blocks PID {child_pid} "
            f"(blocked {child_proc.duration_sec:.1f}s, wait: {wait_label})"
        )
        if child_proc.query:
            child_q = child_proc.query.replace("\n", " ").strip()[:80]
            tree_lines.append(f"{indent}  query: {child_q}")

        child_max = _traverse_blocking_tree(
            parent_pid=child_pid,
            blocking_to_blocked=blocking_to_blocked,
            processes_by_pid=processes_by_pid,
            all_blocked=all_blocked,
            tree_lines=tree_lines,
            indent=indent + "    ",
        )
        if child_max > max_duration:
            max_duration = child_max
    return max_duration


def build_lock_contention_report(
    rows: list[dict[str, Any]],
    database: str = "localhost",
    observed_at: datetime | None = None,
) -> LockContentionReport:
    """Reconstructs the multi-process blocking trees from pg_blocking_pids evidence."""
    obs_time = observed_at or datetime.now(timezone.utc)
    if not rows:
        return LockContentionReport(
            observed_at=obs_time,
            database=database,
            total_blocked_processes=0,
            distinct_root_blockers=0,
            trees=[],
            raw_edges=[],
        )

    edges: list[BlockingEdge] = []
    processes_by_pid: dict[int, LockProcess] = {}
    blocked_to_blocking: dict[int, int] = {}
    blocking_to_blocked: dict[int, list[int]] = defaultdict(list)

    for r in rows:
        b_pid = int(r["blocked_pid"])
        k_pid = int(r["blocking_pid"])

        blocked_proc = LockProcess(
            pid=b_pid,
            user=str(r.get("blocked_user", "")),
            application_name=str(r.get("blocked_app", "")),
            client_addr=str(r.get("blocked_client_addr", "")),
            duration_sec=float(r.get("blocked_duration_sec") or 0.0),
            xact_age_sec=float(r.get("blocked_xact_age_sec") or 0.0),
            state=str(r.get("blocked_state", "")),
            wait_event_type=str(r.get("blocked_wait_event_type", "")),
            wait_event=str(r.get("blocked_wait_event", "")),
            query=str(r.get("blocked_query", "")),
        )

        blocking_proc = LockProcess(
            pid=k_pid,
            user=str(r.get("blocking_user", "")),
            application_name=str(r.get("blocking_app", "")),
            client_addr=str(r.get("blocking_client_addr", "")),
            duration_sec=float(r.get("blocking_duration_sec") or 0.0),
            xact_age_sec=float(r.get("blocking_xact_age_sec") or 0.0),
            state=str(r.get("blocking_state", "")),
            wait_event_type=str(r.get("blocking_wait_event_type", "")),
            wait_event=str(r.get("blocking_wait_event", "")),
            query=str(r.get("blocking_query", "")),
        )

        processes_by_pid[b_pid] = blocked_proc
        processes_by_pid[k_pid] = blocking_proc
        blocked_to_blocking[b_pid] = k_pid
        blocking_to_blocked[k_pid].append(b_pid)

        edges.append(
            BlockingEdge(
                blocking_pid=k_pid,
                blocked_pid=b_pid,
                blocked_process=blocked_proc,
                blocking_process=blocking_proc,
            )
        )

    # Root blockers: PIDs that are blocking others, but are NOT blocked themselves
    root_pids = [pid for pid in blocking_to_blocked if pid not in blocked_to_blocking]

    # In case of cycles, fallback to any unvisited blocking pid
    if not root_pids and blocking_to_blocked:
        root_pids = list(blocking_to_blocked.keys())[:1]

    trees: list[LockTree] = []

    for root_pid in root_pids:
        root_proc = processes_by_pid[root_pid]
        all_blocked: list[int] = []
        tree_lines: list[str] = [
            f"PID {root_pid} [{root_proc.user}@{root_proc.application_name or 'backend'}] "
            f"({root_proc.state or 'active'}, xact_age: {root_proc.xact_age_sec:.1f}s)"
        ]
        if root_proc.query:
            short_q = root_proc.query.replace("\n", " ").strip()[:80]
            tree_lines.append(f"  query: {short_q}")

        max_duration = _traverse_blocking_tree(
            parent_pid=root_pid,
            blocking_to_blocked=blocking_to_blocked,
            processes_by_pid=processes_by_pid,
            all_blocked=all_blocked,
            tree_lines=tree_lines,
        )

        trees.append(
            LockTree(
                root_pid=root_pid,
                root_process=root_proc,
                total_blocked=len(all_blocked),
                blocked_pids=all_blocked,
                max_blocked_duration_sec=max_duration,
                chain_representation="\n".join(tree_lines),
            )
        )

    # Sort trees by max blocked duration descending
    trees.sort(key=lambda t: t.max_blocked_duration_sec, reverse=True)

    return LockContentionReport(
        observed_at=obs_time,
        database=database,
        total_blocked_processes=len(blocked_to_blocking),
        distinct_root_blockers=len(root_pids),
        trees=trees,
        raw_edges=edges,
    )


def lock_report_to_findings(
    report: LockContentionReport,
    server_version: str = "unknown",
) -> list[Finding]:
    """Converts root blocking trees into canonical domain Findings."""
    findings: list[Finding] = []

    for tree in report.trees:
        root = tree.root_process
        if "idle in transaction" in root.state.lower() or tree.max_blocked_duration_sec > 60:
            severity = Severity.CRITICAL
        elif tree.max_blocked_duration_sec > 10 or tree.total_blocked >= 3:
            severity = Severity.HIGH
        else:
            severity = Severity.MEDIUM

        values: dict[str, JsonValue] = {
            "root_pid": tree.root_pid,
            "root_user": root.user,
            "root_state": root.state,
            "root_xact_age_sec": root.xact_age_sec,
            "total_blocked": tree.total_blocked,
            "blocked_pids": [int(p) for p in tree.blocked_pids],
            "max_blocked_duration_sec": tree.max_blocked_duration_sec,
            "chain_text": tree.chain_representation,
        }

        eid = compute_evidence_id("pg_stat", "lock_contention", values)
        evidence = Evidence(
            source="pg_stat",
            observed_at=report.observed_at,
            server_version=server_version,
            query_name="lock_contention",
            values=values,
            evidence_id=eid,
        )

        finding_id = compute_stable_finding_id(
            "PG-LOCK-CONTENTION", "process", f"PID_{tree.root_pid}"
        )
        reason = (
            f"Process PID {tree.root_pid} ({root.state or 'active'}) is holding locks "
            f"blocking {tree.total_blocked} process(es) "
            f"for up to {tree.max_blocked_duration_sec:.1f}s"
        )

        findings.append(
            Finding(
                finding_id=finding_id,
                check="lock_contention",
                severity=severity,
                object_type="process",
                object_name=f"PID {tree.root_pid}",
                reason=reason,
                evidence=[evidence],
            )
        )

    return findings


def render_lock_report_text(report: LockContentionReport) -> str:
    """Visual text summary of database lock contention."""
    if report.total_blocked_processes == 0:
        return "LOCK CONTENTION: 0 blocked processes (clean)."

    lines = [
        "=" * 64,
        f"POSTGRESQL LOCK CONTENTION REPORT: {report.database}",
        f"Total Blocked Processes: {report.total_blocked_processes}",
        f"Distinct Root Blockers: {report.distinct_root_blockers}",
        "=" * 64,
    ]

    for idx, tree in enumerate(report.trees, 1):
        lines.append(
            f"\n--- BLOCKING TREE #{idx} (Max Wait: {tree.max_blocked_duration_sec:.1f}s) ---"
        )
        lines.append(tree.chain_representation)

    lines.append("=" * 64)
    return "\n".join(lines)
