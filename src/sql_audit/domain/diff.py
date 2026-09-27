"""Domain models for differential audit analysis (Diff Engine).

Computes deterministic deltas between two AuditReports using stable
`finding_id` and runtime `evidence_id`.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from sql_audit.domain.models import Finding


class FindingLifecycleState(str, Enum):
    """Lifecycle state of a finding across two audit runs."""

    NEW = "new"
    RESOLVED = "resolved"
    PERSISTING_UNCHANGED = "persisting_unchanged"
    PERSISTING_CHANGED = "persisting_changed"


class ChangedFinding(BaseModel):
    """Represents a finding that persists across audits but whose runtime evidence changed."""

    model_config = ConfigDict(frozen=True)

    finding_id: str
    check: str
    object_name: str
    previous_evidence_ids: list[str]
    current_evidence_ids: list[str]
    current_finding: Finding

    @property
    def previous_evidence_id(self) -> str:
        """Backward-compatible scalar view of the first previous evidence ID."""
        return self.previous_evidence_ids[0] if self.previous_evidence_ids else ""

    @property
    def current_evidence_id(self) -> str:
        """Backward-compatible scalar view of the first current evidence ID."""
        return self.current_evidence_ids[0] if self.current_evidence_ids else ""


class DiffSummary(BaseModel):
    """Quantitative summary of findings lifecycle deltas."""

    model_config = ConfigDict(frozen=True)

    new_count: int = 0
    resolved_count: int = 0
    unchanged_count: int = 0
    changed_count: int = 0
    total_previous: int = 0
    total_current: int = 0


class AuditDiff(BaseModel):
    """Root contract for differential audit analysis."""

    model_config = ConfigDict(frozen=True)

    previous_audit_id: str
    current_audit_id: str
    compared_at: datetime
    database: str
    summary: DiffSummary
    new_findings: list[Finding] = Field(default_factory=list)
    resolved_findings: list[Finding] = Field(default_factory=list)
    unchanged_findings: list[Finding] = Field(default_factory=list)
    changed_findings: list[ChangedFinding] = Field(default_factory=list)
    is_partial: bool = False
