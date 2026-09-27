"""Differential comparison engine between two AuditReports."""

from datetime import datetime, timezone

from sql_audit.domain.diff import AuditDiff, ChangedFinding, DiffSummary
from sql_audit.domain.models import AuditReport, Finding


def compare_audit_reports(
    previous: AuditReport,
    current: AuditReport,
    compared_at: datetime | None = None,
) -> AuditDiff:
    """Computes deterministic deltas between two AuditReports."""
    comp_time = compared_at or datetime.now(timezone.utc)

    prev_map: dict[str, Finding] = {f.finding_id: f for f in previous.findings}
    curr_map: dict[str, Finding] = {f.finding_id: f for f in current.findings}

    new_findings: list[Finding] = []
    unchanged_findings: list[Finding] = []
    changed_findings: list[ChangedFinding] = []
    resolved_findings: list[Finding] = []

    for fid, curr_f in curr_map.items():
        if fid not in prev_map:
            new_findings.append(curr_f)
        else:
            prev_f = prev_map[fid]
            prev_eids = sorted(e.evidence_id for e in prev_f.evidence)
            curr_eids = sorted(e.evidence_id for e in curr_f.evidence)

            if prev_eids == curr_eids:
                unchanged_findings.append(curr_f)
            else:
                changed_findings.append(
                    ChangedFinding(
                        finding_id=fid,
                        check=curr_f.check,
                        object_name=curr_f.object_name,
                        previous_evidence_ids=prev_eids,
                        current_evidence_ids=curr_eids,
                        current_finding=curr_f,
                    )
                )

    for fid, prev_f in prev_map.items():
        if fid not in curr_map:
            resolved_findings.append(prev_f)

    summary = DiffSummary(
        new_count=len(new_findings),
        resolved_count=len(resolved_findings),
        unchanged_count=len(unchanged_findings),
        changed_count=len(changed_findings),
        total_previous=len(previous.findings),
        total_current=len(current.findings),
    )

    return AuditDiff(
        previous_audit_id=previous.audit_id,
        current_audit_id=current.audit_id,
        compared_at=comp_time,
        database=current.database,
        summary=summary,
        new_findings=new_findings,
        resolved_findings=resolved_findings,
        unchanged_findings=unchanged_findings,
        changed_findings=changed_findings,
        is_partial=bool(previous.is_partial or current.is_partial),
    )


def render_diff_text(diff: AuditDiff) -> str:
    """Formats an AuditDiff into a human-readable and agent-friendly report."""
    lines: list[str] = [
        "=" * 64,
        f"AUDIT DIFF REPORT: {diff.database}",
        f"Previous: {diff.previous_audit_id} -> Current: {diff.current_audit_id}",
        f"Summary: +{diff.summary.new_count} new, -{diff.summary.resolved_count} resolved, "
        f"~{diff.summary.changed_count} changed, ={diff.summary.unchanged_count} unchanged",
        "=" * 64,
    ]

    if diff.new_findings:
        lines.append("\n[+] NEW FINDINGS:")
        for f in diff.new_findings:
            lines.append(f"  + [{f.severity.upper()}] {f.check}: {f.object_name} ({f.finding_id})")

    if diff.resolved_findings:
        lines.append("\n[-] RESOLVED FINDINGS:")
        for f in diff.resolved_findings:
            lines.append(f"  - [{f.severity.upper()}] {f.check}: {f.object_name} ({f.finding_id})")

    if diff.changed_findings:
        lines.append("\n[~] PERSISTING FINDINGS WITH CHANGED EVIDENCE:")
        for cf in diff.changed_findings:
            lines.append(f"  ~ {cf.check}: {cf.object_name}")
            prev_str = ", ".join(cf.previous_evidence_ids) if cf.previous_evidence_ids else "(none)"
            curr_str = ", ".join(cf.current_evidence_ids) if cf.current_evidence_ids else "(none)"
            lines.append(f"      prev: {prev_str}")
            lines.append(f"      curr: {curr_str}")

    if diff.unchanged_findings:
        lines.append(
            f"\n[=] {len(diff.unchanged_findings)} unchanged findings (evidence identical)"
        )

    lines.append("=" * 64)
    return "\n".join(lines)
