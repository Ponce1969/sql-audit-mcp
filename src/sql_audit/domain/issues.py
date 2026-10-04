"""Domain DTOs and health report containers for PostgreSQL health audit."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field
from pydantic.dataclasses import dataclass

from sql_audit.domain.models import (
    DEFAULT_MIN_SIZE_BYTES,
    DEFAULT_MIN_TABLE_ROWS,
    AuditReport,
)

ALL_CHECKS: list[str] = [
    "redundant",
    "hot",
    "low-usage",
    "invalid",
    "unindexed-fks",
    "dead-tuples",
]

CHECK_FIELDS: dict[str, str] = {
    "redundant": "redundant_indexes",
    "hot": "hot_issues",
    "low-usage": "low_usage_indexes",
    "invalid": "invalid_indexes",
    "unindexed-fks": "unindexed_fks",
    "dead-tuples": "autovacuum_dead_tuples",
}


class CheckName(str, Enum):
    INVALID_INDEXES = "invalid_indexes"
    UNINDEXED_FKS = "unindexed_fks"
    AUTOVACUUM_DEAD_TUPLES = "autovacuum_dead_tuples"
    HOT_FILLFACTOR = "hot_fillfactor"
    REDUNDANT_INDEXES = "redundant_indexes"
    LOW_USAGE_INDEXES = "low_usage_indexes"


@dataclass(frozen=True)
class HotUpdateIssue:
    table_name: str
    total_updates: int
    hot_updates: int
    hot_ratio_pct: float
    fillfactor: int
    fillfactor_warning: bool
    table_rows: int
    table_size: str


@dataclass(frozen=True)
class RedundantIndexIssue:
    table_name: str
    redundant_index: str
    redundant_size: str
    covering_index: str
    redundant_def: str
    covering_def: str


@dataclass(frozen=True)
class LowUsageIndexIssue:
    table_name: str
    index_name: str
    size: str
    index_scans: int
    table_writes: int
    read_write_ratio: float
    table_rows: int
    table_size: str


@dataclass(frozen=True)
class InvalidIndexIssue:
    child_table: str
    invalid_index: str
    index_size: str


@dataclass(frozen=True)
class UnindexedFKIssue:
    child_table: str
    fk_name: str
    parent_table: str
    fk_definition: str


@dataclass(frozen=True)
class DeadTuplesIssue:
    table_name: str
    dead_tuples: int
    live_tuples: int
    dead_tuple_pct: float
    last_autovacuum: datetime | None = None
    last_vacuum: datetime | None = None


@dataclass(frozen=True)
class DatabaseHealthReport:
    hot_issues: list[HotUpdateIssue] = Field(default_factory=list)
    redundant_indexes: list[RedundantIndexIssue] = Field(default_factory=list)
    low_usage_indexes: list[LowUsageIndexIssue] = Field(default_factory=list)
    invalid_indexes: list[InvalidIndexIssue] = Field(default_factory=list)
    unindexed_fks: list[UnindexedFKIssue] = Field(default_factory=list)
    autovacuum_dead_tuples: list[DeadTuplesIssue] = Field(default_factory=list)
    checks_executed: list[str] = Field(default_factory=list)
    checks_failed: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    is_partial: bool = False

    @property
    def has_critical_issues(self) -> bool:
        return bool(self.invalid_indexes or self.unindexed_fks or self.autovacuum_dead_tuples)

    @property
    def is_healthy(self) -> bool:
        has_any_finding = bool(
            self.hot_issues
            or self.redundant_indexes
            or self.low_usage_indexes
            or self.invalid_indexes
            or self.unindexed_fks
            or self.autovacuum_dead_tuples
        )
        return not self.is_partial and len(self.errors) == 0 and not has_any_finding

    def to_audit_report(
        self,
        database: str = "localhost",
        checks: list[str] | None = None,
        observed_at: datetime | None = None,
        server_version: str = "unknown",
        duration_ms: float = 0.0,
        schemas: list[str] | None = None,
        min_size_bytes: int = DEFAULT_MIN_SIZE_BYTES,
        min_table_rows: int = DEFAULT_MIN_TABLE_ROWS,
        audit_id: str | None = None,
    ) -> AuditReport:
        from sql_audit.application.mapping import convert_legacy_report_to_audit_report

        return convert_legacy_report_to_audit_report(
            report=self,
            database=database,
            checks=checks or (self.checks_executed if self.checks_executed else list(ALL_CHECKS)),
            observed_at=observed_at,
            server_version=server_version,
            duration_ms=duration_ms,
            schemas=schemas,
            min_size_bytes=min_size_bytes,
            min_table_rows=min_table_rows,
            audit_id=audit_id,
        )


class PostgresHealthReport(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    database_alias: str
    schemas_audited: list[str]
    execution_time_ms: float = 0.0
    has_critical_issues: bool = False
    invalid_indexes: list[InvalidIndexIssue] = Field(default_factory=list)
    unindexed_fks: list[UnindexedFKIssue] = Field(default_factory=list)
    autovacuum_dead_tuples: list[DeadTuplesIssue] = Field(default_factory=list)
    hot_fillfactor_issues: list[HotUpdateIssue] = Field(default_factory=list)
    redundant_indexes: list[RedundantIndexIssue] = Field(default_factory=list)
    low_usage_indexes: list[LowUsageIndexIssue] = Field(default_factory=list)
    checks_executed: list[str] = Field(default_factory=list)
    checks_failed: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    is_partial: bool = False

    @property
    def is_healthy(self) -> bool:
        has_any_finding = bool(
            self.invalid_indexes
            or self.unindexed_fks
            or self.autovacuum_dead_tuples
            or self.hot_fillfactor_issues
            or self.redundant_indexes
            or self.low_usage_indexes
        )
        return not self.is_partial and len(self.errors) == 0 and not has_any_finding

    def to_audit_report(
        self,
        database: str | None = None,
        checks: list[str] | None = None,
        observed_at: datetime | None = None,
        server_version: str = "unknown",
        min_size_bytes: int = DEFAULT_MIN_SIZE_BYTES,
        min_table_rows: int = DEFAULT_MIN_TABLE_ROWS,
        audit_id: str | None = None,
    ) -> AuditReport:
        from sql_audit.application.mapping import convert_legacy_report_to_audit_report

        effective_checks = (
            checks
            or (self.checks_executed if self.checks_executed else [c.value for c in CheckName])
        )
        return convert_legacy_report_to_audit_report(
            report=self,
            database=database or self.database_alias,
            checks=effective_checks,
            observed_at=observed_at,
            server_version=server_version,
            duration_ms=self.execution_time_ms,
            schemas=self.schemas_audited,
            min_size_bytes=min_size_bytes,
            min_table_rows=min_table_rows,
            audit_id=audit_id,
        )


__all__ = [
    "ALL_CHECKS",
    "CHECK_FIELDS",
    "CheckName",
    "DatabaseHealthReport",
    "DeadTuplesIssue",
    "HotUpdateIssue",
    "InvalidIndexIssue",
    "LowUsageIndexIssue",
    "PostgresHealthReport",
    "RedundantIndexIssue",
    "UnindexedFKIssue",
]
