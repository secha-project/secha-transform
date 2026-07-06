"""The canonical row — one long-format measurement produced by the engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CanonicalRow:
    """One row of `canonical.measurement` (long format)."""

    measurement_id: str
    source_vendor: str
    source_dataset: str
    device_id: str
    ts_utc: str | None
    quantity: str
    phase: str
    variant: str
    harmonic_order: int | None
    value: float
    unit: str
    aggregation: str
    interval_s: int | None
    quality: str
    source_row_id: str | None
    schema_version: str
    ingested_at: str
    location_id: str | None = None
    session_id: str | None = None
    ts_session_offset_s: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TransformStats:
    """Run statistics: every validation outcome is counted, never silent."""

    records_in: int = 0
    records_rejected: int = 0
    rows_emitted: int = 0
    rows_suspect: int = 0
    rows_dropped: int = 0
    cells_null_skipped: int = 0


@dataclass(frozen=True)
class TransformResult:
    """The engine's output: canonical rows plus the run's validation statistics."""

    rows: list[CanonicalRow]
    stats: TransformStats
