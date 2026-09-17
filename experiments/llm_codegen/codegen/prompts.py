"""Prompts for program generation and repair, built only from metadata and synthetic data.

Every function here that could place data in front of a model first checks that the case it
was given is synthetic. A real case reaching a prompt is a programming error, and it fails
loudly rather than leaking a measured value to a commercial endpoint.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from codegen.cases import Case
from codegen.compare import CaseScore, examples
from codegen.sandbox import Execution

SYSTEM = (
    "You are a senior data engineer. You write small, correct, dependency-free Python programs "
    "that implement a written specification exactly. Reply with a single ```python code block "
    "that contains the complete program."
)

CONSTRAINTS = (
    "Constraints: Python 3.11 or later with the standard library only. Read the JSON input from "
    "standard input and write the JSON output to standard output. Do not read or write files, "
    "open network connections, start processes or read environment variables. Use "
    "datetime.timezone.utc, because the time zone database is not installed."
)

REPAIR = (
    "Your program was run on the example input and did not produce the expected output.\n\n"
    "{feedback}\n\n"
    "Fix the program. Reply with the complete corrected program in a single ```python code block."
)

# Enough rows to show every rule without spending the context window of a 16k-token model on
# near-identical lines. Smaller outputs are always shown in full.
EXAMPLE_ROW_LIMIT = 16

_INTERPRETER_SECTION = "## 8. Interpreter mode"


def _require_synthetic(case: Case) -> None:
    if case.real_data:
        raise ValueError(f"{case.case_id} holds real data and must never be placed in a prompt")


def rulebook_text(rulebook: dict[str, Any]) -> str:
    return yaml.safe_dump(
        rulebook, sort_keys=False, allow_unicode=True, width=100, default_flow_style=None
    ).strip()


def _json_list(items: list[dict[str, Any]], indent: str) -> str:
    body = ",\n".join(f"{indent}  {json.dumps(item, ensure_ascii=False)}" for item in items)
    return f"[\n{body}\n{indent}]"


def example_rows(case: Case) -> tuple[list[dict[str, Any]], str]:
    """All expected rows when they are few, otherwise a subset that still shows every rule.

    The subset keeps the first row of each quantity within each record, plus every suspect row,
    so each transform, default and flag appears at least once. The statistics always describe
    the full output, so a program can still check its own row count against them.
    """
    rows = case.expected_rows
    if len(rows) <= EXAMPLE_ROW_LIMIT:
        return rows, "Expected output on standard output (rows may be in any order):"
    seen: set[tuple[Any, ...]] = set()
    subset = []
    for row in rows:
        key = (row["source_row_id"], row["quantity"])
        if key not in seen or row["quality"] != "ok":
            seen.add(key)
            subset.append(row)
    if len(subset) > EXAMPLE_ROW_LIMIT:
        raise ValueError(
            f"{case.case_id}: {len(subset)} example rows exceed the limit of "
            f"{EXAMPLE_ROW_LIMIT}; build the development case from fewer records"
        )
    label = (
        f"Expected output on standard output, rows in any order. Only {len(subset)} of the "
        f"{len(rows)} expected rows are shown: the first row of each quantity for each record, "
        "and every suspect row. The statistics describe the full output."
    )
    return subset, label


def _example(case: Case, mode: str) -> str:
    rulebook_line = (
        '  "rulebook": <the rulebook above, as a JSON object>,\n' if mode == "interpreter" else ""
    )
    rows, label = example_rows(case)
    return (
        "Input on standard input:\n"
        "{\n"
        f"{rulebook_line}"
        f'  "device_factors": {json.dumps(case.device_factors)},\n'
        f'  "records": {_json_list(case.records, "  ")}\n'
        "}\n\n"
        f"{label}\n"
        "{\n"
        f'  "rows": {_json_list(rows, "  ")},\n'
        f'  "stats": {json.dumps(case.expected_stats)}\n'
        "}"
    )


def snapshot_messages(contract: str, case: Case) -> list[dict[str, str]]:
    """Ask for a program with one vendor's configuration written into it."""
    _require_synthetic(case)
    vendor = case.rulebook["vendor"]
    body = contract.split(_INTERPRETER_SECTION)[0].strip()
    user = "\n\n".join(
        [
            f"Write a Python program that transforms raw records from the vendor `{vendor}` "
            "into canonical rows.",
            "The program must implement the contract below for this vendor's rulebook, which "
            "follows it in full. Write this vendor's configuration into the program itself: when "
            "it runs, the program receives only the records and the device factors, never the "
            "rulebook.",
            f"<contract>\n{body}\n</contract>",
            f"<rulebook>\n{rulebook_text(case.rulebook)}\n</rulebook>",
            f"<example>\n{_example(case, 'snapshot')}\n</example>",
            CONSTRAINTS,
            "Reply with the complete program in a single ```python code block.",
        ]
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def interpreter_messages(contract: str, case: Case) -> list[dict[str, str]]:
    """Ask for a program that derives all vendor behaviour from the rulebook it is given."""
    _require_synthetic(case)
    user = "\n\n".join(
        [
            "Write a Python program that transforms raw records into canonical rows for any "
            "vendor, driven entirely by the rulebook it receives on standard input, as section 8 "
            "of the contract describes.",
            "The rulebook below is one example of what the program will receive. The program "
            "will also be run with other vendors' rulebooks, including sources of a different "
            "shape, and with edited versions of this one, so it must not contain constants "
            "specific to this vendor.",
            f"<contract>\n{contract.strip()}\n</contract>",
            f"<rulebook>\n{rulebook_text(case.rulebook)}\n</rulebook>",
            f"<example>\n{_example(case, 'interpreter')}\n</example>",
            CONSTRAINTS,
            "Reply with the complete program in a single ```python code block.",
        ]
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def repair_messages(
    messages: list[dict[str, str]], reply: str, feedback: str
) -> list[dict[str, str]]:
    return [
        *messages,
        {"role": "assistant", "content": reply},
        {"role": "user", "content": REPAIR.format(feedback=feedback)},
    ]


def feedback_for(
    case: Case, execution: Execution | None, score: CaseScore, output: Any, extracted: bool
) -> str:
    """What a developer would see after running the program on the example input."""
    _require_synthetic(case)
    if not extracted or execution is None:
        return "Your reply did not contain a Python code block."
    if execution.status in {"guard_rejected", "syntax_error"}:
        return "The program was not run: " + "; ".join(execution.violations) + "."
    if execution.status == "timeout":
        return f"The program did not finish: it {execution.detail}."
    if execution.status == "crashed":
        return (
            f"The program exited with an error ({execution.detail}). "
            f"The end of its error output was:\n{execution.stderr[-1500:]}"
        )
    if execution.status == "bad_output":
        return f"The program's output could not be read: {execution.detail}."

    lines = [
        f"Expected {score.oracle_rows} rows. The program produced {score.script_rows}, "
        f"of which {score.exact_rows} match exactly."
    ]
    if score.output_error:
        lines.append(score.output_error + ".")
    if score.field_errors:
        lines.append(
            "Rows with the right identity but a wrong field, counted by field: "
            f"{dict(score.field_errors)}."
        )
    if score.near_miss_fields:
        lines.append(
            "Rows that look like the same reading with a different identity field, "
            f"counted by field: {dict(score.near_miss_fields)}."
        )
    if score.stats_diff:
        lines.append(f"Statistics that differ, as [expected, produced]: {score.stats_diff}.")
    diff = examples(case.expected_rows, output, limit=3)
    for pair in diff["mismatched"]:
        lines.append(
            f"Expected row: {json.dumps(pair['expected'])}\n"
            f"Produced row: {json.dumps(pair['produced'])}"
        )
    for row in diff["missing"]:
        lines.append(f"Expected but not produced: {json.dumps(row)}")
    for row in diff["extra"]:
        lines.append(f"Produced but not expected: {json.dumps(row)}")
    return "\n".join(lines)
