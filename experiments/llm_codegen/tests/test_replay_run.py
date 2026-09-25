"""The Kempower replay's measures, controls and guards, on synthetic inputs only.

No test here runs the reference interpreter or a recorded program on a Kempower input: what
they write there is what the registered predictions are about.
"""

from __future__ import annotations

import copy
import socket
from pathlib import Path

import pytest
from codegen import client
from codegen.replay_run import (
    LEVELS,
    POSITIVE_PATCHES,
    REFERENCE_SHA256,
    NetworkRefused,
    beyond_contract,
    contract_exact,
    engine_reproduces,
    frozen_sources_unchanged,
    identity_counts,
    negative_control,
    network_is_refused,
    no_network,
    ordered_differences,
    patched,
    positive_control,
    projected_match,
    replay_program,
    sha256_text_file,
    summarise_program,
)
from codegen.sandbox import execute

EXPERIMENT = Path(__file__).resolve().parents[1]
REFERENCE = EXPERIMENT / "reference" / "interpreter.py"
IDENTITY_FIELDS = [
    "source_vendor",
    "device_id",
    "ts_utc",
    "quantity",
    "phase",
    "variant",
    "harmonic_order",
    "aggregation",
    "source_row_id",
]


def _row(**changes):
    row = {
        "measurement_id": "a" * 32,
        "source_vendor": "demo",
        "source_dataset": "demo_dataset",
        "device_id": "demo:device",
        "ts_utc": None,
        "quantity": "voltage",
        "phase": "dc",
        "variant": "none",
        "harmonic_order": None,
        "value": 375.0,
        "unit": "V",
        "aggregation": "average",
        "interval_s": 10,
        "quality": "ok",
        "source_row_id": "part=1:0",
        "schema_version": "1.1.0",
    }
    row.update(changes)
    return row


def _soc(**changes):
    soc = {
        "quantity": "state_of_charge",
        "phase": "none",
        "unit": "percent",
        "value": 20.0,
        "aggregation": "instantaneous",
        "measurement_id": "b" * 32,
    }
    return _row(**(soc | changes))


ENGINE = [_row(), _soc()]


def test_each_level_sets_aside_one_more_construct():
    exact = {"rows": copy.deepcopy(ENGINE)}
    own_aggregation = {
        "rows": [
            _row(source_row_id=None, measurement_id="c" * 32),
            _soc(source_row_id=None, measurement_id="c" * 32),
        ]
    }
    contract_like = {
        "rows": [
            _row(source_row_id=None, measurement_id="c" * 32),
            _soc(source_row_id=None, measurement_id="c" * 32, aggregation="average"),
        ]
    }

    def reached(output):
        return [
            level for level, left_out in LEVELS.items() if projected_match(ENGINE, output, left_out)
        ]

    assert reached(exact) == [1, 2, 3]
    assert reached(own_aggregation) == [2, 3]
    assert reached(contract_like) == [3]


def test_levels_count_rows_and_compare_values_with_the_tolerance():
    def matches(rows):
        return projected_match(ENGINE, {"rows": rows}, LEVELS[1])

    assert matches([_row(value=375.0 + 1e-12), _soc()])
    assert not matches([_row(value=375.001), _soc()])
    assert not matches([_row(), _soc(), _soc()])  # a duplicate is an extra row
    assert not matches([_row(value="375.0"), _soc()])  # a value must be a number
    assert not matches([{k: v for k, v in _row().items() if k != "phase"}, _soc()])
    assert not projected_match(ENGINE, None, LEVELS[3])
    assert not projected_match(ENGINE, {"rows": "no"}, LEVELS[3])


def test_identity_counts_distinct_ids_against_rows():
    assert identity_counts({"rows": [_row(), _row(), _soc()]}) == {"rows": 3, "distinct_ids": 2}
    assert identity_counts("not an object") == {"rows": 0, "distinct_ids": 0}


def test_beyond_contract_counts_only_agreement_with_the_engine_where_the_reference_differs():
    reference = [_row(source_row_id=None), _soc(source_row_id=None, aggregation="average")]
    follows_engine = {"rows": [_row(), _soc()]}
    follows_contract = {"rows": reference}

    assert beyond_contract(ENGINE, reference, follows_engine) == {
        "aggregation": 1,
        "source_row_id": 2,
    }
    assert beyond_contract(ENGINE, reference, follows_contract) == {
        "aggregation": 0,
        "source_row_id": 0,
    }
    assert beyond_contract(ENGINE, ENGINE, follows_engine) == {"aggregation": 0, "source_row_id": 0}
    assert beyond_contract(ENGINE, None, follows_engine) is None


def test_ordered_differences_name_each_field_and_the_quantities_of_aggregation():
    produced = [
        _row(source_row_id=None, measurement_id="c" * 32),
        _soc(source_row_id=None, measurement_id="c" * 32, aggregation="average"),
    ]
    assert ordered_differences(ENGINE, produced) == {
        "fields": {"measurement_id": 2, "source_row_id": 2, "aggregation": 1},
        "aggregation_by_quantity": {"state_of_charge": 1},
    }
    assert ordered_differences(ENGINE, produced[:1]) is None


def test_contract_exact_needs_rows_statistics_and_identity_hashes():
    score = {"passed": True, "stats_equal": True, "id_matches": 4, "exact_rows": 4}
    assert contract_exact(score)
    assert not contract_exact(score | {"id_matches": 3})
    assert not contract_exact(score | {"stats_equal": False})


def test_the_controls_patch_the_frozen_reference_exactly_once():
    reference = REFERENCE.read_text(encoding="utf-8")
    assert sha256_text_file(REFERENCE) == REFERENCE_SHA256
    positive = positive_control(reference)
    assert 'column.get("aggregation", default_aggregation)' in positive
    assert 'record.get("_payload_position")' in positive
    assert 'if quantity == "voltage"' in negative_control(reference)
    with pytest.raises(ValueError, match="no longer applies"):
        patched("print('a different reference')", POSITIVE_PATCHES)


def test_the_controls_add_exactly_the_constructs_they_name():
    """On a synthetic vendor with the two constructs, not on Kempower."""
    rulebook = {
        "vendor": "demo",
        "source_schema": {
            "shape": "wide",
            "record": {"row_id_from": "payload_position", "device_id_template": "demo:device"},
            "defaults": {"aggregation": "average", "interval_s": 10},
        },
        "mapping": {
            "source": "demo_dataset",
            "target_schema_version": "1.1.0",
            "columns": [
                {"src": "v", "quantity": "voltage", "phase": "dc", "unit": "V"},
                {
                    "src": "s",
                    "quantity": "state_of_charge",
                    "phase": "none",
                    "unit": "percent",
                    "aggregation": "instantaneous",
                },
            ],
        },
        "validation": {
            "rules": [
                {
                    "quantity": "voltage",
                    "type": "range",
                    "min": 0,
                    "max": 1000,
                    "on_fail": "flag_suspect",
                }
            ]
        },
        "target": {"measurement_id_from": IDENTITY_FIELDS},
    }
    payload = {
        "records": [{"v": -1.0, "s": 20.0, "_payload_position": "part=1:0"}],
        "device_factors": {},
        "rulebook": rulebook,
    }
    reference = REFERENCE.read_text(encoding="utf-8")

    positive = execute(positive_control(reference), payload).output
    negative = execute(negative_control(reference), payload).output

    by_quantity = {row["quantity"]: row for row in positive["rows"]}
    assert by_quantity["state_of_charge"]["aggregation"] == "instantaneous"
    assert by_quantity["voltage"]["aggregation"] == "average"
    assert {row["source_row_id"] for row in positive["rows"]} == {"part=1:0"}
    assert by_quantity["voltage"]["quality"] == "suspect"
    assert {row["quantity"]: row["quality"] for row in negative["rows"]}["voltage"] == "ok"


def test_no_network_refuses_every_connection_and_restores_the_socket_module():
    original = socket.create_connection
    with no_network():
        assert network_is_refused()
        with pytest.raises(NetworkRefused):
            socket.create_connection(("127.0.0.1", 9), timeout=1)
        with pytest.raises(NetworkRefused):
            socket.socket().connect(("127.0.0.1", 9))
    assert socket.create_connection is original


def _mx_dev(synthetic_cases):
    return [c for c in synthetic_cases if c.vendor == "mx_electrix" and c.split == "dev"]


def test_a_program_is_scored_against_both_oracles(synthetic_cases):
    cases = _mx_dev(synthetic_cases)
    reference = REFERENCE.read_text(encoding="utf-8")
    first = replay_program(reference, "interpreter", cases, {}, 60)
    outputs = {r["case"]: r["output"] for r in first}

    exact = summarise_program(replay_program(reference, "interpreter", cases, outputs, 60))
    broken = summarise_program(replay_program("print('no')", "interpreter", cases, outputs, 60))
    missing = replay_program(None, "interpreter", cases, outputs, 60)

    assert exact["runs"] and exact["engine_pass"] and exact["contract_exact"]
    assert exact["level"] == 1 and exact["engine_micro"]["f1"] == 1.0
    assert broken["statuses"] == {"bad_output": 1} and broken["level"] is None
    assert broken["contract_exact"] is False
    assert missing[0]["status"] == "not_run"
    assert summarise_program(first)["contract_exact"] is None  # no reference output to compare


def test_the_replay_constructs_no_model_client(synthetic_cases, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the replay constructed a model client")

    monkeypatch.setattr(client.ChatClient, "__init__", refuse)
    reference = REFERENCE.read_text(encoding="utf-8")
    with no_network():
        results = replay_program(reference, "interpreter", _mx_dev(synthetic_cases), {}, 60)
    assert results[0]["status"] == "ok"


def test_the_engine_check_names_a_case_it_does_not_reproduce(metadata_root, synthetic_cases):
    assert engine_reproduces(metadata_root, synthetic_cases) == []
    tampered = copy.deepcopy(synthetic_cases[0])
    tampered.expected_stats["records_in"] += 1
    assert engine_reproduces(metadata_root, [tampered]) == [tampered.case_id]


def test_a_changed_contract_or_reference_is_named(metadata_root, tmp_path):
    changed = tmp_path / "CONTRACT.md"
    changed.write_text("a different contract", encoding="utf-8")
    problems = frozen_sources_unchanged(EXPERIMENT.parents[1], metadata_root, changed, changed)
    assert "CONTRACT.md differs from the frozen contract" in problems
    assert "reference/interpreter.py differs from the frozen reference" in problems
