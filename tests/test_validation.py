"""Validation application: the fifth metadata type drives the engine.

Synthetic tests pin rule semantics; the golden tests prove the REAL mx_electrix
validation.yaml governs the engine (out-of-range -> suspect, missing timestamp -> reject).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from secha_transform.engine.transform import transform_records
from secha_transform.metadata.loader import MetadataBundle, load_bundle
from test_transform_unit import _bundle

RECORD = {"meter": 7, "ts": "2025-01-01T00:00:00", "id": 1, "v": 230.0, "f": 50.0}
FACTORS = {"7": {"uk": 1.0, "ik": 1.0}}


def _validated_bundle(rules: list[dict[str, Any]]) -> MetadataBundle:
    return _bundle(validation={"vendor": "demo", "rules": rules})


def test_clean_data_stays_ok() -> None:
    rules = [
        {"quantity": "frequency", "type": "range", "min": 45, "max": 65, "on_fail": "flag_suspect"}
    ]
    result = transform_records([RECORD], _validated_bundle(rules), FACTORS)
    assert all(r.quality == "ok" for r in result.rows)
    assert result.stats.rows_suspect == 0


def test_range_failure_flags_suspect_but_keeps_value() -> None:
    rules = [
        {"quantity": "frequency", "type": "range", "min": 45, "max": 65, "on_fail": "flag_suspect"}
    ]
    record = dict(RECORD, f=70.0)
    result = transform_records([record], _validated_bundle(rules), FACTORS)
    by_quantity = {r.quantity: r for r in result.rows}
    assert by_quantity["frequency"].quality == "suspect"
    assert by_quantity["frequency"].value == 70.0  # flagged, never mutated
    assert by_quantity["voltage"].quality == "ok"  # other rows untouched
    assert result.stats.rows_suspect == 1


def test_record_not_null_rejects_whole_record() -> None:
    rules = [{"field": "ts", "type": "not_null", "on_fail": "reject_row"}]
    record = {"meter": 7, "id": 1, "v": 230.0, "f": 50.0}  # ts missing
    result = transform_records([record], _validated_bundle(rules), FACTORS)
    assert result.rows == []
    assert result.stats.records_rejected == 1


def test_quantity_drop_row_drops_only_that_row() -> None:
    rules = [
        {"quantity": "frequency", "type": "range", "min": 45, "max": 65, "on_fail": "drop_row"}
    ]
    record = dict(RECORD, f=70.0)
    result = transform_records([record], _validated_bundle(rules), FACTORS)
    assert {r.quantity for r in result.rows} == {"voltage"}
    assert result.stats.rows_dropped == 1


def test_quantity_reject_row_discards_whole_record() -> None:
    rules = [
        {"quantity": "frequency", "type": "range", "min": 45, "max": 65, "on_fail": "reject_row"}
    ]
    record = dict(RECORD, f=70.0)
    result = transform_records([record], _validated_bundle(rules), FACTORS)
    assert result.rows == []
    assert result.stats.records_rejected == 1
    assert result.stats.rows_suspect == 0


def test_unimplemented_rule_type_raises() -> None:
    rules = [{"quantity": "frequency", "type": "enum", "on_fail": "flag_suspect"}]
    with pytest.raises(ValueError, match="not implemented"):
        transform_records([RECORD], _validated_bundle(rules), FACTORS)


def test_unimplemented_on_fail_raises() -> None:
    rules = [
        {"quantity": "frequency", "type": "range", "min": 45, "max": 65, "on_fail": "quarantine"}
    ]
    with pytest.raises(ValueError, match="not implemented"):
        transform_records([RECORD], _validated_bundle(rules), FACTORS)


def test_entity_value_not_null_rule_is_accepted() -> None:
    """The real config carries this rule; it is satisfied by construction, never an error."""
    rules = [{"entity": "measurement", "field": "value", "type": "not_null", "on_fail": "drop_row"}]
    result = transform_records([RECORD], _validated_bundle(rules), FACTORS)
    assert len(result.rows) == 2


# --- golden: the REAL mx_electrix validation.yaml governs the engine ------------------------


def test_real_config_flags_out_of_range_frequency(metadata_root: Path) -> None:
    bundle = load_bundle(metadata_root, "mx_electrix")
    record = {"id": 1, "meter": 22, "timestamp": "2025-05-21T00:00:00", "fhz": 70.0, "dl1": 0.89}
    result = transform_records([record], bundle, {"22": {"uk": 1.0, "ik": 1.0}})
    by_quantity = {r.quantity: r for r in result.rows}
    assert by_quantity["frequency"].quality == "suspect"  # 70 Hz outside 45-65
    assert by_quantity["frequency"].value == 70.0
    assert by_quantity["thd_voltage"].quality == "ok"
    assert result.stats.rows_suspect == 1


def test_real_config_rejects_record_without_timestamp(metadata_root: Path) -> None:
    bundle = load_bundle(metadata_root, "mx_electrix")
    record = {"id": 1, "meter": 22, "fhz": 50.0}  # timestamp missing
    result = transform_records([record], bundle, {"22": {"uk": 1.0, "ik": 1.0}})
    assert result.rows == []
    assert result.stats.records_rejected == 1
