"""Domain models for PostgreSQL lock contention and blocking trees."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LockProcess(BaseModel):
    """Represents a database process involved in a lock contention graph."""

    model_config = ConfigDict(frozen=True)

    pid: int
    user: str = ""
    application_name: str = ""
    client_addr: str = ""
    duration_sec: float = 0.0
    xact_age_sec: float = 0.0
    state: str = ""
    wait_event_type: str = ""
    wait_event: str = ""
    query: str = ""


class BlockingEdge(BaseModel):
    """Direct edge indicating blocking_pid blocks blocked_pid."""

    model_config = ConfigDict(frozen=True)

    blocking_pid: int
    blocked_pid: int
    blocked_process: LockProcess
    blocking_process: LockProcess


class LockTree(BaseModel):
    """Tree of processes blocked directly or transitively by a root blocking PID."""

    model_config = ConfigDict(frozen=True)

    root_pid: int
    root_process: LockProcess
    total_blocked: int
    blocked_pids: list[int]
    max_blocked_duration_sec: float
    chain_representation: str = Field(description="Formatted visual ASCII blocking tree")


class LockContentionReport(BaseModel):
    """Comprehensive snapshot of database lock contention."""

    model_config = ConfigDict(frozen=True)

    observed_at: datetime
    database: str
    total_blocked_processes: int
    distinct_root_blockers: int
    trees: list[LockTree] = Field(default_factory=list)
    raw_edges: list[BlockingEdge] = Field(default_factory=list)
