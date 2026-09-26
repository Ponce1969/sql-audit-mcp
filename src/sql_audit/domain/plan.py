"""Domain models for PostgreSQL query plan analysis and impact simulation."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from sql_audit.domain.models import JsonValue


class PlanRiskLevel(str, Enum):
    """Risk severity for plan anomalies."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PlanWarning(BaseModel):
    """Specific bottleneck or warning detected in execution plan."""

    model_config = ConfigDict(frozen=True)

    warning_type: str
    risk_level: PlanRiskLevel
    node_type: str
    relation_name: str = ""
    message: str


class PlanSummary(BaseModel):
    """Key quantitative metrics extracted from the execution plan."""

    model_config = ConfigDict(frozen=True)

    total_cost: float
    planning_time_ms: float | None = None
    execution_time_ms: float | None = None
    shared_hit_blocks: int = 0
    shared_read_blocks: int = 0
    temp_written_blocks: int = 0
    has_seq_scan_large_table: bool = False
    has_disk_sort: bool = False
    has_high_estimation_skew: bool = False
    warnings: list[PlanWarning] = Field(default_factory=list)


class PlanAnalysisReport(BaseModel):
    """Structured report evaluating PostgreSQL execution plan."""

    model_config = ConfigDict(frozen=True)

    observed_at: datetime
    query: str
    is_analyzed: bool = False
    database: str = "localhost"
    summary: PlanSummary
    raw_plan: dict[str, JsonValue] = Field(default_factory=dict)


__all__ = [
    "PlanAnalysisReport",
    "PlanRiskLevel",
    "PlanSummary",
    "PlanWarning",
]
