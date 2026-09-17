"""Prompts carry only metadata and synthetic data, and show the rulebook the oracle used."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
import yaml
from codegen.compare import score_case
from codegen.prompts import (
    EXAMPLE_ROW_LIMIT,
    example_rows,
    feedback_for,
    interpreter_messages,
    snapshot_messages,
)
from codegen.sandbox import Execution

CONTRACT = (Path(__file__).resolve().parents[1] / "CONTRACT.md").read_text(encoding="utf-8")


def dev_case(cases: list, vendor: str):
    return next(c for c in cases if c.vendor == vendor and c.split == "dev")


def test_every_prompt_builder_refuses_real_data(synthetic_cases):
    real = dataclasses.replace(dev_case(synthetic_cases, "mx_electrix"), real_data=True)
    with pytest.raises(ValueError, match="real data"):
        snapshot_messages(CONTRACT, real)
    with pytest.raises(ValueError, match="real data"):
        interpreter_messages(CONTRACT, real)
    with pytest.raises(ValueError, match="real data"):
        score = score_case(real.expected_rows, real.expected_stats, None)
        feedback_for(real, Execution(status="crashed"), score, None, extracted=True)


def test_development_cases_are_synthetic(synthetic_cases):
    assert all(not c.real_data for c in synthetic_cases if c.split == "dev")


@pytest.mark.parametrize("vendor", ["mx_electrix", "procem_kampusareena_pq"])
def test_the_prompt_shows_exactly_the_rulebook_the_oracle_used(synthetic_cases, vendor):
    case = dev_case(synthetic_cases, vendor)
    content = snapshot_messages(CONTRACT, case)[1]["content"]
    block = re.search(r"<rulebook>\n(.*?)\n</rulebook>", content, re.DOTALL)
    assert block is not None
    assert yaml.safe_load(block.group(1)) == case.rulebook


def test_only_interpreter_mode_is_told_about_interpreter_mode(synthetic_cases):
    case = dev_case(synthetic_cases, "mx_electrix")
    assert "## 8. Interpreter mode" not in snapshot_messages(CONTRACT, case)[1]["content"]
    assert "## 8. Interpreter mode" in interpreter_messages(CONTRACT, case)[1]["content"]


def test_a_large_example_shows_a_labelled_subset_that_keeps_every_suspect_row(synthetic_cases):
    case = dev_case(synthetic_cases, "mx_electrix")
    assert len(case.expected_rows) > EXAMPLE_ROW_LIMIT
    rows, label = example_rows(case)
    assert len(rows) <= EXAMPLE_ROW_LIMIT
    assert f"of the {len(case.expected_rows)} expected rows" in label
    suspect = [r for r in case.expected_rows if r["quality"] != "ok"]
    assert suspect and all(r in rows for r in suspect)
    assert {r["quantity"] for r in rows} == {r["quantity"] for r in case.expected_rows}


def test_a_small_example_is_shown_in_full(synthetic_cases):
    case = dev_case(synthetic_cases, "procem_kampusareena_pq")
    rows, label = example_rows(case)
    assert rows == case.expected_rows and "Only" not in label


def test_no_measured_value_reaches_any_prompt(frozen_cases):
    """Every decimal with four or more digits in a real record must be absent from prompts."""
    measured: set[str] = set()
    for case in frozen_cases:
        if case.real_data:
            for record in case.records:
                for value in record.values():
                    measured.update(re.findall(r"-?\d+\.\d{4,}", str(value)))
    assert measured, "expected real records to contain measured values"
    for vendor in ("mx_electrix", "procem_kampusareena_pq"):
        case = dev_case(frozen_cases, vendor)
        for build in (snapshot_messages, interpreter_messages):
            text = "\n".join(m["content"] for m in build(CONTRACT, case))
            leaked = {value for value in measured if value in text}
            assert not leaked, f"measured values in a {vendor} prompt: {sorted(leaked)[:5]}"
