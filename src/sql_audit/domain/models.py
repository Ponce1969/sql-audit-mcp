"""Domain models for deterministic PostgreSQL health audit.

Strictly typed with zero `Any` usage.
Decouples diagnostic evidence from remediation actions.
"""

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

__all__ = [
    "AuditReport",
    "Evidence",
    "ExecutionMetadata",
    "Finding",
    "JsonValue",
    "Severity",
]


class Severity(str, Enum):
    """Categorical severity based on database operational impact."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Evidence(BaseModel):
    """Authoritative database state observed at a specific point in time."""

    model_config = ConfigDict(frozen=True)

    source: Literal["pg_catalog", "pg_stat", "information_schema"]
    observed_at: datetime
    server_version: str
    query_name: str
    values: dict[str, JsonValue]
    evidence_id: str = Field(description="Deterministic SHA-256 digest of observed runtime values")


class Finding(BaseModel):
    """Stable architectural or operational problem diagnosed in the database.

    `finding_id` is stable across executions and does not change when
    counters or metrics fluctuate.
    """

    model_config = ConfigDict(frozen=True)

    finding_id: str = Field(
        description="Stable identifier of the problem on target object (counter-independent)"
    )
    check: str
    severity: Severity
    object_type: str
    object_name: str
    reason: str
    evidence: list[Evidence] = Field(default_factory=list)


class ExecutionMetadata(BaseModel):
    """Metadata regarding audit execution parameters and performance."""

    model_config = ConfigDict(frozen=True)

    duration_ms: float = 0.0
    schemas: list[str] | None = None
    min_size_bytes: int = 0
    min_table_rows: int = 10000


class AuditReport(BaseModel):
    """Root canonical contract for both CLI and MCP consumers."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = "1.0.0"
    audit_id: str = Field(description="Unique audit run execution identifier")
    database: str
    server_version: str = "unknown"
    observed_at: datetime
    execution_metadata: ExecutionMetadata = Field(default_factory=ExecutionMetadata)
    checks_executed: list[str]
    has_critical_issues: bool
    summary: dict[str, int] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
