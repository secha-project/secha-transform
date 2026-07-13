"""Self-contained engine unit tests (no secha-metadata needed): scaling, skips, invariants."""

from __future__ import annotations

from typing import Any

import pytest

from secha_transform.engine.models import CanonicalRow
from secha_transform.engine.transform import transform_records
from secha_transform.metadata.loader import MetadataBundle


def _bundle(validation: dict[str, Any] | None = None) -> MetadataBundle:
    return MetadataBundle(
        vendor="demo",
        canonical={},
        vocabulary={},
        units={},
        transforms={},
        target={"measurement_id_from": ["source_vendor", "device_id", "quantity", "phase"]},
        source_schema={
            "record": {
                "meter_field": "meter",
                "timestamp_field": "ts",
                "row_id_field": "id",
                "device_id_template": "demo:meter:{meter}",
            },
            "defaults": {"aggregation": "average", "interval_s": 60},
            "device_factors": {"voltage_factor": "uk", "current_factor": "ik"},
        },
        mapping={
            "source": "measurements",
            "target_schema_version": "1.0.0",
            "columns": [
                {
                    "src": "v",
                    "quantity": "voltage",
                    "phase": "L1",
                    "unit": "V",
                    "transform": "scale_by_factor",
                    "args": {"factor_field": "uk"},
                },
                {
                    "src": "f",
                    "quantity": "frequency",
                    "phase": "none",
                    "unit": "Hz",
                    "transform": "none",
                },
            ],
        },
        validation=validation or {},
    )


def _rows(
    records: list[dict[str, Any]],
    bundle: MetadataBundle,
    factors: dict[str, dict[str, float]] | None = None,
) -> list[CanonicalRow]:
    return transform_records(records, bundle, factors or {}).rows


def test_scaling_multiplies_by_device_factor() -> None:
    rows = _rows(
        [{"meter": 7, "ts": "2025-01-01T00:00:00", "id": 1, "v": 230.0, "f": 50.0}],
        _bundle(),
        {"7": {"uk": 2.0, "ik": 1.0}},
    )
    by_quantity = {r.quantity: r for r in rows}
    assert by_quantity["voltage"].value == 460.0  # 230 * uk(2.0)
    assert by_quantity["frequency"].value == 50.0
    assert by_quantity["voltage"].device_id == "demo:meter:7"
    assert by_quantity["voltage"].aggregation == "average"
    assert by_quantity["voltage"].interval_s == 60
    assert by_quantity["voltage"].ts_utc == "2025-01-01T00:00:00Z"


def test_missing_factor_skips_scaled_column() -> None:
    rows = _rows([{"meter": 7, "ts": "t", "id": 1, "v": 230.0, "f": 50.0}], _bundle())
    assert {r.quantity for r in rows} == {"frequency"}  # voltage skipped (no factor)


def test_null_cell_is_skipped_and_counted() -> None:
    result = transform_records(
        [{"meter": 7, "ts": "t", "id": 1, "v": None, "f": 50.0}],
        _bundle(),
        {"7": {"uk": 1.0, "ik": 1.0}},
    )
    assert {r.quantity for r in result.rows} == {"frequency"}
    assert result.stats.cells_null_skipped == 1


def test_idempotent_measurement_id_is_stable() -> None:
    record = {"meter": 7, "ts": "2025-01-01T00:00:00", "id": 1, "v": 230.0, "f": 50.0}
    a = _rows([record], _bundle(), {"7": {"uk": 1.0, "ik": 1.0}})
    b = _rows([record], _bundle(), {"7": {"uk": 1.0, "ik": 1.0}})
    assert [r.measurement_id for r in a] == [r.measurement_id for r in b]


def test_unknown_transform_raises() -> None:
    """A library-legal transform the engine does not implement must fail loudly, not float()."""
    bundle = _bundle()
    bundle.mapping["columns"] = [
        {"src": "v", "quantity": "voltage", "phase": "L1", "unit": "V", "transform": "slugify"}
    ]
    with pytest.raises(ValueError, match="not implemented"):
        transform_records([{"meter": 7, "ts": "t", "id": 1, "v": 230.0}], bundle, {})


def test_parse_decimal_handles_comma_locale() -> None:
    """The EE-documented locale case: comma decimal separators parse correctly."""
    bundle = _bundle()
    bundle.mapping["columns"] = [
        {
            "src": "v",
            "quantity": "voltage",
            "phase": "L1",
            "unit": "V",
            "transform": "parse_decimal",
            "args": {"decimal_separator": ","},
        }
    ]
    rows = _rows([{"meter": 7, "ts": "2025-01-01T00:00:00", "id": 1, "v": "1,25"}], bundle)
    assert rows[0].value == 1.25


def test_timezone_aware_timestamp_passes_through() -> None:
    """Negative offsets must not get a fabricated Z appended."""
    rows = _rows(
        [{"meter": 7, "ts": "2025-01-01T00:00:00-05:00", "id": 1, "f": 50.0}],
        _bundle(),
        {"7": {"uk": 1.0, "ik": 1.0}},
    )
    assert rows[0].ts_utc == "2025-01-01T00:00:00-05:00"


def test_unparseable_timestamp_yields_none() -> None:
    rows = _rows(
        [{"meter": 7, "ts": "not-a-time", "id": 1, "f": 50.0}],
        _bundle(),
        {"7": {"uk": 1.0, "ik": 1.0}},
    )
    assert rows[0].ts_utc is None


def test_single_ingested_at_per_run() -> None:
    records = [
        {"meter": 7, "ts": "2025-01-01T00:00:00", "id": 1, "v": 230.0, "f": 50.0},
        {"meter": 7, "ts": "2025-01-01T00:01:00", "id": 2, "v": 231.0, "f": 50.0},
    ]
    rows = _rows(records, _bundle(), {"7": {"uk": 1.0, "ik": 1.0}})
    assert len({r.ingested_at for r in rows}) == 1


# --- long-shape sources (`rows:` mapping keyed by field value) --------------------------


def _long_bundle(datetime_format: str = "epoch_ms") -> MetadataBundle:
    return MetadataBundle(
        vendor="demo_long",
        canonical={},
        vocabulary={},
        units={},
        transforms={},
        target={"measurement_id_from": ["source_vendor", "quantity", "ts_utc", "aggregation"]},
        source_schema={
            "shape": "long",
            "format": {"type": "csv", "delimiter": "\t", "datetime_format": datetime_format},
            "record": {
                "key_field": "measurement_id",
                "value_field": "value",
                "timestamp_field": "timestamp",
                "row_id_field": "measurement_id",
                "device_id_template": "demo:site:device",
            },
            "defaults": {"aggregation": "average", "interval_s": 1},
        },
        mapping={
            "source": "daily_dump",
            "target_schema_version": "1.0.0",
            "rows": [
                {"key": "1", "quantity": "voltage", "phase": "L1", "unit": "V"},
                {
                    "key": "2",
                    "quantity": "energy_active_import",
                    "phase": "none",
                    "aggregation": "counter",
                    "unit": "kWh",
                },
            ],
        },
        validation={},
    )


def test_long_lookup_emits_one_row_per_record() -> None:
    records = [{"measurement_id": "1", "value": "230.5", "timestamp": "1781470800246"}]
    result = transform_records(records, _long_bundle())
    (row,) = result.rows
    assert row.quantity == "voltage"
    assert row.value == 230.5
    assert row.ts_utc == "2026-06-14T21:00:00.246Z"  # exact epoch-ms rendering, no rounding
    assert row.device_id == "demo:site:device"
    assert row.source_row_id == "1"
    assert row.aggregation == "average"


def test_long_unmapped_key_is_skipped_and_counted() -> None:
    records = [{"measurement_id": "99", "value": "1.0", "timestamp": "1781470800246"}]
    result = transform_records(records, _long_bundle())
    assert result.rows == []
    assert result.stats.records_unmapped == 1


def test_long_per_row_aggregation_override() -> None:
    records = [{"measurement_id": "2", "value": "36349.897", "timestamp": "1781470800246"}]
    result = transform_records(records, _long_bundle())
    assert result.rows[0].aggregation == "counter"


def test_epoch_ms_garbage_timestamp_yields_none() -> None:
    records = [{"measurement_id": "1", "value": "230.5", "timestamp": "not-epoch"}]
    result = transform_records(records, _long_bundle())
    assert result.rows[0].ts_utc is None


def test_epoch_s_format_renders_whole_seconds() -> None:
    records = [{"measurement_id": "1", "value": "230.5", "timestamp": "1781470800"}]
    result = transform_records(records, _long_bundle(datetime_format="epoch_s"))
    assert result.rows[0].ts_utc == "2026-06-14T21:00:00Z"


def test_unknown_datetime_format_raises() -> None:
    records = [{"measurement_id": "1", "value": "230.5", "timestamp": "1781470800246"}]
    with pytest.raises(ValueError, match="datetime_format"):
        transform_records(records, _long_bundle(datetime_format="stardate"))
