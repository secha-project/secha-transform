"""The comparison must never call a wrong output right, and must say how it is wrong."""

from __future__ import annotations

from codegen.compare import STATS_KEYS, examples, instant, score_case

STATS = dict.fromkeys(STATS_KEYS, 0) | {"records_in": 1, "rows_emitted": 1}


def row(**overrides) -> dict:
    base = {
        "measurement_id": "a" * 32,
        "source_vendor": "vendor",
        "source_dataset": "dataset",
        "device_id": "vendor:device",
        "ts_utc": "2026-06-14T21:00:00.246Z",
        "quantity": "voltage",
        "phase": "L1",
        "variant": "none",
        "harmonic_order": None,
        "value": 230.5,
        "unit": "V",
        "aggregation": "average",
        "interval_s": 1,
        "quality": "ok",
        "source_row_id": "23524",
        "schema_version": "1.0.0",
    }
    return base | overrides


def output(rows: list, stats: dict | None = None) -> dict:
    return {"rows": rows, "stats": STATS if stats is None else stats}


def test_identical_output_passes_on_every_tier():
    score = score_case([row()], STATS, output([row()]))
    assert score.passed and score.stats_equal
    assert score.f1 == 1.0 and score.id_matches == 1 and score.lenient_rows == 1


def test_row_order_is_not_part_of_the_contract():
    a, b = row(), row(phase="L2", measurement_id="b" * 32)
    assert score_case([a, b], STATS, output([b, a])).passed


def test_a_missing_row_fails_and_is_counted():
    score = score_case([row(), row(phase="L2")], STATS, output([row()]))
    assert not score.passed
    assert score.missing == 1 and score.recall == 0.5 and score.precision == 1.0


def test_an_extra_row_fails_and_is_counted():
    score = score_case([row()], STATS, output([row(), row(phase="L2")]))
    assert not score.passed
    assert score.extra == 1 and score.precision == 0.5


def test_duplicates_count_as_a_multiset():
    score = score_case([row(), row()], STATS, output([row()]))
    assert not score.passed and score.missing == 1


def test_value_agrees_within_floating_point_noise():
    assert score_case([row(value=230.5)], STATS, output([row(value=230.5 + 1e-12)])).passed


def test_a_wrong_value_is_attributed_to_the_value():
    score = score_case([row(value=230.5)], STATS, output([row(value=230.6)]))
    assert not score.passed and dict(score.field_errors) == {"value": 1}


def test_a_numeric_string_is_not_a_number():
    score = score_case([row(value=230.5)], STATS, output([row(value="230.5")]))
    assert not score.passed and dict(score.field_errors) == {"value": 1}


def test_a_wrong_unit_is_attributed_to_the_unit():
    score = score_case([row()], STATS, output([row(unit="kV")]))
    assert dict(score.field_errors) == {"unit": 1}


def test_a_wrong_identity_field_is_diagnosed_as_a_near_miss():
    score = score_case([row(variant="fundamental")], STATS, output([row(variant="none")]))
    assert not score.passed
    assert score.missing == 1 and score.extra == 1
    assert dict(score.near_miss_fields) == {"variant": 1}


def test_same_instant_in_another_format_fails_strictly_but_passes_leniently():
    produced = row(ts_utc="2026-06-14T21:00:00.246000+00:00")
    score = score_case([row()], STATS, output([produced]))
    assert not score.passed
    assert score.lenient_rows == score.oracle_rows == 1


def test_statistics_are_scored_separately_from_rows():
    score = score_case([row()], STATS, output([row()], STATS | {"records_in": 2}))
    assert score.passed and not score.stats_equal
    assert score.stats_diff == {"records_in": [1, 2]}


def test_a_different_identity_hash_does_not_fail_the_rows():
    score = score_case([row()], STATS, output([row(measurement_id="b" * 32)]))
    assert score.passed and score.id_matches == 0


def test_output_of_the_wrong_shape_fails():
    for bad in (None, [], {"rows": "nope"}, "text"):
        score = score_case([row()], STATS, bad)
        assert not score.passed and score.output_error


def test_a_row_without_identity_fields_is_malformed():
    broken = {key: value for key, value in row().items() if key != "phase"}
    score = score_case([row()], STATS, output([row(), broken]))
    assert not score.passed and score.malformed_rows == 1


def test_examples_show_the_expected_and_produced_rows_side_by_side():
    diff = examples([row()], output([row(unit="kV")]))
    assert diff["mismatched"][0]["produced"]["unit"] == "kV"
    assert diff["missing"] == [] and diff["extra"] == []


def test_a_naive_timestamp_is_the_same_instant_as_utc():
    assert instant("2025-08-15T00:00:00") == instant("2025-08-15T00:00:00Z")
    assert instant("not a time") == "not a time" and instant(None) is None
