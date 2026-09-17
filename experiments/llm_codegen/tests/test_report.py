"""The report: headline tables and failure summaries built from recorded samples."""

from __future__ import annotations

from typing import Any

from codegen.report import build_report


def _attempt(passed: bool, stats_diff: dict[str, Any], unseen: bool | None = None) -> dict:
    return {
        "extracted": True,
        "generation": {"elapsed_s": 12.0, "finish_reason": "stop", "truncation_retries": 1},
        "summary": {
            "runs": True,
            "test_pass": passed,
            "lenient_pass": passed,
            "stats_pass": not stats_diff,
            "id_pass": passed,
            "drift_survival": 0.5,
            "unseen_test_pass": unseen,
            "test_micro": {"f1": 1.0 if passed else 0.5},
        },
        "cases": [
            {
                "split": "test",
                "status": "ok",
                "score": {
                    "missing": 3,
                    "extra": 1,
                    "field_errors": {"value": 2},
                    "near_miss_fields": {"variant": 1},
                    "stats_diff": stats_diff,
                },
            },
            {
                "split": "dev",
                "status": "crashed",
                "score": {
                    "missing": 40,
                    "extra": 0,
                    "field_errors": {"unit": 9},
                    "near_miss_fields": {},
                    "stats_diff": {},
                },
            },
        ],
    }


def _sample(mode: str, attempt: dict) -> dict:
    return {"mode": mode, "seen_vendor": "mx_electrix", "model": "m", "attempts": [attempt]}


def test_statistics_that_differ_are_counted_by_name_not_by_value():
    samples = [
        _sample("snapshot", _attempt(False, {"rows_emitted": [41, 40], "rows_suspect": [2, 1]})),
        _sample("snapshot", _attempt(True, {"rows_emitted": [41, 39]})),
    ]
    report = build_report({"meta": {"n": 2}, "samples": samples})
    assert "statistics that differ {'rows_emitted': 2, 'rows_suspect': 1}" in report
    assert "| snapshot | mx_electrix | `m` | 2 | 100% | 100% | 50% | 100% |" in report


def test_failures_count_test_inputs_only():
    report = build_report({"samples": [_sample("snapshot", _attempt(False, {}))]})
    assert "test executions {'ok': 1}" in report
    assert "rows missing 3, extra 1" in report  # a wrong identity is named by no field
    assert "wrong fields {'value': 2}" in report
    assert "wrong identity fields {'variant': 1}" in report
    assert "statistics that differ none" in report
    assert "prompts resent because the provider truncated them 1" in report


def test_interpreter_programs_report_the_vendor_they_were_not_shown():
    samples = [
        _sample("interpreter", _attempt(True, {}, unseen=False)),
        _sample("interpreter", _attempt(True, {}, unseen=True)),
    ]
    report = build_report({"samples": samples})
    assert "| mx_electrix | `m` | 100% | 50% | 50% |" in report
