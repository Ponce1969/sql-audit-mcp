"""Domain models for PostgreSQL table and index physical bloat estimation."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TableBloat(BaseModel):
    """Estimated physical bloat for a table heap."""

    model_config = ConfigDict(frozen=True)

    schema_name: str
    table_name: str
    table_size_bytes: int
    expected_size_bytes: int
    bloat_bytes: int
    bloat_ratio_pct: float
    is_bloated: bool = False


class IndexBloat(BaseModel):
    """Estimated physical bloat for a B-tree index."""

    model_config = ConfigDict(frozen=True)

    schema_name: str
    table_name: str
    index_name: str
    index_size_bytes: int
    expected_size_bytes: int
    bloat_bytes: int
    bloat_ratio_pct: float
    is_bloated: bool = False


class BloatReport(BaseModel):
    """Aggregate physical bloat estimation report for tables and indexes."""

    model_config = ConfigDict(frozen=True)

    observed_at: datetime
    database: str
    min_bloat_bytes: int = 10_000_000
    min_bloat_ratio_pct: float = 20.0
    tables: list[TableBloat] = Field(default_factory=list)
    indexes: list[IndexBloat] = Field(default_factory=list)
    total_table_bloat_bytes: int = 0
    total_index_bloat_bytes: int = 0


__all__ = [
    "BloatReport",
    "IndexBloat",
    "TableBloat",
]
