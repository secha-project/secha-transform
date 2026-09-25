"""The registered predictions are judged by code, on hand-built results: each one must be
seen to hold when it holds and to fail when it does not."""

from __future__ import annotations

import copy

from codegen.replay_report import build_replay_report, evaluate_predictions

SHOWN = ("mx_electrix", "procem_kampusareena_pq")


def _summary(**changes):
    summary = {
        "runs": True,
        "engine_pass": False,
        "engine_pass_on_any_input": False,
        "contract_exact": False,
        "contract_rows_matched": 0,
        "level": None,
        "max_distinct_ids": 5,
        "beyond_contract": {"aggregation": 0, "source_row_id": 0},
        "statuses": {"ok": 3},
    }
    return summary | changes


def _program(run, mode, shown, sample, exact=False):
    role = "floor" if mode == "snapshot" else "secondary" if run == "phi4-14b" else "primary"
    return {
        "run": run,
        "mode": mode,
        "seen_vendor": shown,
        "sample": sample,
        "attempt": 0,
        "role": role,
        "key": f"{run}/{mode}/{shown}/s{sample}",
        "results": [],
        "summary": _summary(contract_exact=exact, level=3 if exact else None),
    }


def _reference_case(name, engine_rows, own):
    return {
        "case": f"kempower/test/{name}",
        "rows": engine_rows,
        "distinct_ids": 5,
        "quantities": 5,
        "engine_rows": engine_rows,
        "engine_distinct_ids": engine_rows,
        "differences": {
            "fields": {
                "source_row_id": engine_rows,
                "measurement_id": engine_rows,
                "aggregation": sum(own.values()),
            },
            "aggregation_by_quantity": dict(own),
        },
        "own_aggregation_rows": dict(own),
        "stats_equal": True,
    }


def _result():
    """A result in which every prediction holds."""
    exact = {"kimi-k3": 8, "codestral-2508": 3, "phi4-14b": 0}
    programs = []
    for run, count in exact.items():
        for index in range(10):
            shown = SHOWN[index // 5]
            # fill MX Electrix first, so programs shown the wide shape do at least as well
            programs.append(_program(run, "interpreter", shown, index % 5, exact=index < count))
            programs.append(_program(run, "snapshot", shown, index % 5))
    stats = {"rows_suspect": 2, "records_rejected": 1, "cells_null_skipped": 1}
    return {
        "meta": {
            "started_at": "2026-09-26T08:00:00+00:00",
            "finished_at": "2026-09-26T08:05:00+00:00",
            "engine_commit": "abc1234",
            "metadata_commit": "9170d3e",
            "contract_sha256": "325cc47c89a5" + "0" * 52,
            "frozen": {"engine_commit": "5a25726", "metadata_commit": "9170d3e"},
            "rerun_reason": None,
        },
        "gates": [{"gate": f"G{n}", "passed": True, "detail": "ok"} for n in range(1, 8)],
        "cases": [
            {
                "case": f"kempower/test/{name}",
                "records": records,
                "rows": rows,
                "stats": stats,
                "sessions_full_rulebook": 2,
                "input_sha256": "f" * 64,
            }
            for name, records, rows in (
                ("golden_edge", 6, 24),
                ("real_part_start", 200, 990),
                ("real_part_spread", 400, 1980),
            )
        ],
        "reference": {
            "summary": _summary(level=3, statuses={"ok": 3}),
            "cases": [
                _reference_case("golden_edge", 24, {"state_of_charge": 5, "temperature": 4}),
                _reference_case(
                    "real_part_start", 990, {"state_of_charge": 198, "temperature": 196}
                ),
                _reference_case(
                    "real_part_spread", 1980, {"state_of_charge": 396, "temperature": 392}
                ),
            ],
        },
        "programs": programs,
    }


def _held(result):
    return {p["id"]: p["holds"] for p in evaluate_predictions(result)}


def test_every_prediction_holds_on_a_result_built_to_satisfy_it():
    assert _held(_result()) == {"P1": True, "P2": True, "P3": True, "P4": True, "P5": True}


def test_p1_fails_when_anything_passes_against_the_engine_on_one_input():
    result = _result()
    result["programs"][0]["summary"]["engine_pass_on_any_input"] = True
    assert _held(result)["P1"] is False
    result = _result()
    result["reference"]["summary"]["engine_pass_on_any_input"] = True
    assert _held(result)["P1"] is False


def test_p2_fails_on_any_other_difference_or_a_missed_row():
    result = _result()
    result["reference"]["cases"][1]["differences"]["fields"]["unit"] = 1
    assert _held(result)["P2"] is False
    result = _result()
    result["reference"]["cases"][0]["differences"]["aggregation_by_quantity"] = {
        "state_of_charge": 5,
        "temperature": 3,
    }
    assert _held(result)["P2"] is False
    result = _result()
    result["reference"]["summary"]["level"] = 2
    assert _held(result)["P2"] is False
    result = _result()
    result["reference"]["cases"][2]["differences"] = None
    assert _held(result)["P2"] is False


def test_p3_fails_when_identity_does_not_collapse_as_predicted():
    result = _result()
    result["reference"]["cases"][0]["distinct_ids"] = 24
    assert _held(result)["P3"] is False


def test_p4_is_judged_on_the_registered_bounds_and_the_wide_shape_ordering():
    result = _result()
    for program in result["programs"]:
        if program["run"] == "kimi-k3" and program["mode"] == "interpreter":
            program["summary"]["contract_exact"] = program["sample"] < 3  # 6 of 10
    assert _held(result)["P4"] is False

    result = _result()
    for program in result["programs"]:
        if program["run"] == "codestral-2508" and program["mode"] == "interpreter":
            # 3 of 10 still, but all shown ProCem
            program["summary"]["contract_exact"] = (
                program["seen_vendor"] == "procem_kampusareena_pq" and program["sample"] < 3
            )
    assert _held(result)["P4"] is False


def test_p5_fails_when_a_snapshot_program_writes_a_row_of_the_reference():
    result = _result()
    floor = next(p for p in result["programs"] if p["role"] == "floor")
    floor["summary"]["contract_rows_matched"] = 1
    assert _held(result)["P5"] is False
    floor["summary"]["contract_exact"] = None
    assert _held(result)["P5"] is None  # without the reference's output it cannot be judged


def test_the_report_renders_every_section():
    report = build_replay_report(copy.deepcopy(_result()))
    for heading in ("## Gates", "## Cases", "## Predictions", "## Arms", "## Programs"):
        assert heading in report
    assert "| P4 | yes |" in report
