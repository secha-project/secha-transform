"""Score a program's output against the engine's output for the same input.

The engine is the oracle, so this module never decides what is correct. It only measures
how far a candidate's rows and statistics are from the engine's, and it reports the
distance in a way that separates kinds of mistake: a missing row, an extra row, a row with
the right identity but a wrong value or unit, and a row that is almost right but carries the
wrong phase or variant.

Rows are compared as multisets on their identity, because row order is not part of the
contract and duplicates are.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

IDENTITY = (
    "device_id",
    "ts_utc",
    "quantity",
    "phase",
    "variant",
    "harmonic_order",
    "aggregation",
    "source_row_id",
)
EXACT_FIELDS = (
    "source_vendor",
    "source_dataset",
    "unit",
    "interval_s",
    "quality",
    "schema_version",
)
STATS_KEYS = (
    "records_in",
    "records_rejected",
    "records_unmapped",
    "rows_emitted",
    "rows_suspect",
    "rows_dropped",
    "cells_null_skipped",
)
REL_TOL = 1e-9
ABS_TOL = 1e-9


def values_close(expected: Any, produced: Any) -> bool:
    """Numeric equality with a tolerance, so `raw * uk * ik` and `raw * (uk * ik)` agree."""
    if isinstance(produced, bool) or not isinstance(produced, int | float):
        return False
    return math.isclose(float(expected), float(produced), rel_tol=REL_TOL, abs_tol=ABS_TOL)


def instant(ts: Any) -> Any:
    """A timestamp as an absolute instant, or the raw value when it is not a timestamp.

    Used only for the lenient tier: `2026-06-14T21:00:00.246Z` and
    `2026-06-14T21:00:00.246000+00:00` are the same instant written differently.
    """
    if not isinstance(ts, str):
        return ts
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def _key(row: dict[str, Any], lenient: bool) -> tuple[Any, ...]:
    values = [row.get(name) for name in IDENTITY]
    if lenient:
        values[1] = instant(values[1])
    return tuple(values)


def _exact_fields_equal(expected: dict[str, Any], produced: dict[str, Any]) -> bool:
    if not all(expected.get(name) == produced.get(name) for name in EXACT_FIELDS):
        return False
    return values_close(expected["value"], produced.get("value"))


def _field_differences(expected: dict[str, Any], produced: dict[str, Any]) -> list[str]:
    wrong = [name for name in EXACT_FIELDS if expected.get(name) != produced.get(name)]
    if not values_close(expected["value"], produced.get("value")):
        wrong.append("value")
    return wrong


@dataclass
class CaseScore:
    """How one program's output for one input differs from the engine's."""

    oracle_rows: int = 0
    script_rows: int = 0
    malformed_rows: int = 0
    exact_rows: int = 0
    lenient_rows: int = 0
    id_matches: int = 0
    missing: int = 0
    extra: int = 0
    field_errors: Counter[str] = field(default_factory=Counter)
    near_miss_fields: Counter[str] = field(default_factory=Counter)
    stats_equal: bool = False
    stats_diff: dict[str, list[Any]] = field(default_factory=dict)
    output_error: str = ""

    @property
    def passed(self) -> bool:
        """Strict: the same multiset of rows, every field equal, nothing malformed."""
        return (
            not self.output_error
            and self.malformed_rows == 0
            and self.exact_rows == self.oracle_rows == self.script_rows
        )

    @property
    def precision(self) -> float:
        return self.exact_rows / self.script_rows if self.script_rows else 0.0

    @property
    def recall(self) -> float:
        return self.exact_rows / self.oracle_rows if self.oracle_rows else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "oracle_rows": self.oracle_rows,
            "script_rows": self.script_rows,
            "malformed_rows": self.malformed_rows,
            "exact_rows": self.exact_rows,
            "lenient_rows": self.lenient_rows,
            "id_matches": self.id_matches,
            "missing": self.missing,
            "extra": self.extra,
            "field_errors": dict(self.field_errors),
            "near_miss_fields": dict(self.near_miss_fields),
            "stats_equal": self.stats_equal,
            "stats_diff": self.stats_diff,
            "output_error": self.output_error,
            "passed": self.passed,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


def _pair(
    oracle: list[dict[str, Any]], produced: list[dict[str, Any]], lenient: bool
) -> tuple[
    list[tuple[dict[str, Any], dict[str, Any]]],
    list[tuple[dict[str, Any], dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Pair rows with equal identity: exact pairs first, then same-identity mismatches.

    Returns (exact pairs, same-identity pairs whose other fields differ, unpaired oracle rows,
    unpaired produced rows).
    """
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in produced:
        buckets[_key(row, lenient)].append(row)

    exact: list[tuple[dict[str, Any], dict[str, Any]]] = []
    leftover_oracle: list[dict[str, Any]] = []
    for expected in oracle:
        candidates = buckets.get(_key(expected, lenient), [])
        match = next(
            (i for i, row in enumerate(candidates) if _exact_fields_equal(expected, row)), None
        )
        if match is None:
            leftover_oracle.append(expected)
        else:
            exact.append((expected, candidates.pop(match)))

    mismatched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    missing: list[dict[str, Any]] = []
    for expected in leftover_oracle:
        candidates = buckets.get(_key(expected, lenient), [])
        if candidates:
            mismatched.append((expected, candidates.pop(0)))
        else:
            missing.append(expected)
    extra = [row for rows in buckets.values() for row in rows]
    return exact, mismatched, missing, extra


def _near_misses(missing: list[dict[str, Any]], extra: list[dict[str, Any]]) -> Counter[str]:
    """Which identity fields separate rows that are otherwise the same reading.

    A missing engine row and an extra program row with the same device, row id, instant and
    value are almost certainly the same reading given the wrong phase, variant, quantity,
    order or aggregation. Naming the field turns "missing + extra" into a diagnosis.
    """
    by_reading: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in extra:
        by_reading[
            (row.get("device_id"), row.get("source_row_id"), instant(row.get("ts_utc")))
        ].append(row)
    differences: Counter[str] = Counter()
    for expected in missing:
        reading = (expected["device_id"], expected["source_row_id"], instant(expected["ts_utc"]))
        candidates = by_reading.get(reading, [])
        match = next(
            (
                i
                for i, row in enumerate(candidates)
                if values_close(expected["value"], row.get("value"))
            ),
            None,
        )
        if match is None:
            continue
        produced = candidates.pop(match)
        for name in IDENTITY:
            if expected.get(name) != produced.get(name):
                differences[name] += 1
    return differences


def score_case(
    oracle_rows: list[dict[str, Any]], oracle_stats: dict[str, int], output: Any
) -> CaseScore:
    """Compare one program output with the engine's rows and stats for the same input."""
    score = CaseScore(oracle_rows=len(oracle_rows))
    if not isinstance(output, dict) or not isinstance(output.get("rows"), list):
        score.output_error = "output is not an object with a 'rows' list"
        score.missing = len(oracle_rows)
        return score

    rows: list[dict[str, Any]] = []
    for row in output["rows"]:
        if isinstance(row, dict) and all(name in row for name in (*IDENTITY, "value")):
            rows.append(row)
        else:
            score.malformed_rows += 1
    score.script_rows = len(output["rows"])

    exact, mismatched, missing, extra = _pair(oracle_rows, rows, lenient=False)
    score.exact_rows = len(exact)
    score.id_matches = sum(
        1
        for expected, produced in exact
        if expected["measurement_id"] == produced.get("measurement_id")
    )
    for expected, produced in mismatched:
        score.field_errors.update(_field_differences(expected, produced))
    unmatched_extra = extra + [produced for _, produced in mismatched]
    score.missing = len(missing)
    score.extra = len(extra) + score.malformed_rows
    score.near_miss_fields = _near_misses(missing, unmatched_extra)

    lenient_exact, _, _, _ = _pair(oracle_rows, rows, lenient=True)
    score.lenient_rows = len(lenient_exact)

    stats = output.get("stats")
    if isinstance(stats, dict):
        score.stats_diff = {
            key: [oracle_stats.get(key), stats.get(key)]
            for key in STATS_KEYS
            if oracle_stats.get(key) != stats.get(key)
        }
        score.stats_equal = not score.stats_diff
    else:
        score.stats_diff = {"stats": [oracle_stats, stats]}
    return score


def examples(
    oracle_rows: list[dict[str, Any]], output: Any, limit: int = 3
) -> dict[str, list[Any]]:
    """A few concrete differences, in a form a developer could act on."""
    rows: list[dict[str, Any]] = []
    if isinstance(output, dict) and isinstance(output.get("rows"), list):
        rows = [
            r
            for r in output["rows"]
            if isinstance(r, dict) and all(n in r for n in (*IDENTITY, "value"))
        ]
    _, mismatched, missing, extra = _pair(oracle_rows, rows, lenient=False)
    return {
        "mismatched": [{"expected": e, "produced": p} for e, p in mismatched[:limit]],
        "missing": missing[:limit],
        "extra": extra[:limit],
    }
