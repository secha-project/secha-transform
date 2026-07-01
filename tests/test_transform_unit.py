"""Self-contained engine unit tests (no secha-metadata needed) — scaling, skips, invariants."""

from __future__ import annotations

from secha_transform.engine.transform import transform_records
from secha_transform.metadata.loader import MetadataBundle


def _bundle() -> MetadataBundle:
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
        validation={},
    )


def test_scaling_multiplies_by_device_factor() -> None:
    rows = transform_records(
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
    rows = transform_records(
        [{"meter": 7, "ts": "t", "id": 1, "v": 230.0, "f": 50.0}], _bundle(), {}
    )
    assert {r.quantity for r in rows} == {"frequency"}  # voltage skipped (no factor)


def test_null_cell_is_skipped() -> None:
    rows = transform_records(
        [{"meter": 7, "ts": "t", "id": 1, "v": None, "f": 50.0}],
        _bundle(),
        {"7": {"uk": 1.0, "ik": 1.0}},
    )
    assert {r.quantity for r in rows} == {"frequency"}


def test_idempotent_measurement_id_is_stable() -> None:
    record = {"meter": 7, "ts": "2025-01-01T00:00:00", "id": 1, "v": 230.0, "f": 50.0}
    a = transform_records([record], _bundle(), {"7": {"uk": 1.0, "ik": 1.0}})
    b = transform_records([record], _bundle(), {"7": {"uk": 1.0, "ik": 1.0}})
    assert [r.measurement_id for r in a] == [r.measurement_id for r in b]
