"""The harness is tested against programs whose correctness is known before any model runs.

A correct reference must score exact everywhere, a program with a rulebook frozen into it must
be caught by every drift case, and planted defects must be caught. If any of these fails, no
model result produced by the harness can be trusted.
"""

from __future__ import annotations

import harness
import pytest
from codegen.cases import VENDORS

REFERENCE = harness.REFERENCE.read_text(encoding="utf-8")


def test_the_reference_is_exact_on_every_case_including_drift(synthetic_cases):
    results = harness.evaluate(REFERENCE, synthetic_cases, "interpreter", 60.0)
    for result in results:
        score = result["score"]
        assert result["status"] == "ok", result
        assert score["passed"] and score["stats_equal"], result
        assert score["id_matches"] == score["exact_rows"], result


@pytest.mark.parametrize("vendor", VENDORS)
def test_a_frozen_rulebook_passes_its_own_cases_and_fails_every_drift_case(synthetic_cases, vendor):
    own = [c for c in synthetic_cases if c.vendor == vendor and c.split in {"dev", "test"}]
    drift = [c for c in synthetic_cases if c.vendor == vendor and c.split == "drift"]
    frozen = harness.freeze(REFERENCE, next(c for c in own if c.split == "dev").rulebook)
    assert all(r["score"]["passed"] for r in harness.evaluate(frozen, own, "snapshot", 60.0))
    assert drift
    assert not any(r["score"]["passed"] for r in harness.evaluate(frozen, drift, "snapshot", 60.0))


def test_forgetting_the_scaling_factor_is_caught(synthetic_cases):
    old, new = harness.MUTANTS["no_scaling"]
    scored = [c for c in synthetic_cases if c.split in {"dev", "test"}]
    results = harness.evaluate(REFERENCE.replace(old, new), scored, "interpreter", 60.0)
    assert any(not r["score"]["passed"] for r in results)


def test_a_timestamp_format_defect_is_caught_strictly_and_recognised_leniently(synthetic_cases):
    old, new = harness.MUTANTS["no_utc_suffix"]
    scored = [c for c in synthetic_cases if c.split in {"dev", "test"}]
    results = harness.evaluate(REFERENCE.replace(old, new), scored, "interpreter", 60.0)
    format_only = [
        r
        for r in results
        if not r["score"]["passed"]
        and r["score"]["lenient_rows"] == r["score"]["oracle_rows"] == r["score"]["script_rows"]
    ]
    assert format_only


def test_freezing_requires_the_freeze_line():
    with pytest.raises(ValueError):
        harness.freeze("print('no freeze line here')", {})


def test_a_missing_program_is_scored_as_not_run(synthetic_cases):
    results = harness.evaluate(None, synthetic_cases[:2], "snapshot", 60.0)
    assert all(r["status"] == "not_run" and not r["score"]["passed"] for r in results)


def _result(vendor: str, split: str, passed: bool) -> dict:
    score = {
        "passed": passed,
        "stats_equal": passed,
        "exact_rows": 2 if passed else 1,
        "oracle_rows": 2,
        "script_rows": 2,
        "lenient_rows": 2 if passed else 1,
        "malformed_rows": 0,
        "id_matches": 2 if passed else 1,
    }
    return {"vendor": vendor, "split": split, "status": "ok", "score": score}


def test_summary_separates_the_shown_vendor_the_other_vendor_and_drift():
    dev = {"score": {"passed": True, "stats_equal": True}}
    results = [
        _result("mx_electrix", "test", True),
        _result("procem_kampusareena_pq", "test", False),
        _result("mx_electrix", "drift", True),
        _result("mx_electrix", "drift", False),
    ]
    summary = harness.summarise(dev, results, "mx_electrix")
    assert summary["dev_pass"] and summary["test_pass"] and summary["id_pass"]
    assert summary["unseen_test_pass"] is False
    assert summary["drift_survival"] == 0.5
