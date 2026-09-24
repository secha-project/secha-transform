"""The canonical row: one long-format measurement produced by the engine."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

# The reader stamps each record with its place in the landed payload under this key when the
# source declares `record.row_id_from: payload_position` (a source with no row key). The
# leading underscore keeps it clear of real source columns; the engine reads it as the row id.
PAYLOAD_POSITION_FIELD = "_payload_position"


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
        # a shallow read of the fields: every field is a scalar, so this equals asdict(),
        # which deep-copies and took over half of a large run's time
        return {name: getattr(self, name) for name in CANONICAL_ROW_FIELDS}


# field names in declaration order, read once (dataclasses.fields() is slow per call)
CANONICAL_ROW_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(CanonicalRow))


@dataclass
class TransformStats:
    """Run statistics: every validation outcome is counted, never silent."""

    records_in: int = 0
    records_rejected: int = 0
    records_unmapped: int = 0  # long sources: landed keys not (yet) in the mapping rows
    rows_emitted: int = 0
    rows_suspect: int = 0
    rows_dropped: int = 0
    cells_null_skipped: int = 0


@dataclass(frozen=True)
class TransformResult:
    """The engine's output: canonical rows, the run's statistics, and any session rows.

    `sessions` holds one `charging_session` row per distinct session seen in the batch, as a
    dict keyed by the canonical schema's field names (so a new session attribute in the
    schema needs no engine change). Empty for sources without sessions.
    """

    rows: list[CanonicalRow]
    stats: TransformStats
    sessions: list[dict[str, Any]] = field(default_factory=list)
