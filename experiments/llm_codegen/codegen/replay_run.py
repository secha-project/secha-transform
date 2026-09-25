"""Running and scoring the Kempower replay (kempower_replay/PROTOCOL.md, Measures and gates).

Programs run through the September sandbox (`execute`) and are scored by the September scorer
(`score_case`), unchanged, against two oracles: the engine, and the reference interpreter,
whose output on Kempower is what a program that follows the contract exactly would write.
What this module adds is measured on top of those, never instead of them: rows compared on
fewer fields (the levels), identity counts, agreement with the engine beyond the contract, the
two controls, the checks that nothing frozen has moved, and a guard that refuses the network.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from codegen.cases import Case
from codegen.compare import EXACT_FIELDS, IDENTITY, STATS_KEYS, score_case, values_close
from codegen.metrics import micro
from codegen.oracle import run_engine
from codegen.sandbox import execute

ENGINE_COMMIT = "5a25726"
METADATA_COMMIT = "9170d3e"
FROZEN_METADATA_PATHS = ("canonical", "vendors/kempower", "targets", "tests/fixtures/kempower")
CONTRACT_SHA256 = "325cc47c89a5cb5e862f7e3c29a300178ae5657e0cdee78bd4a71624a44acc11"
REFERENCE_SHA256 = "f8cf502eb3984e205dd61296ee4ece8c4c478807cca8e23c989ddbda7cb263c3"

# every field of a contract row except the value, which is compared with a tolerance
SCORED_FIELDS = ("measurement_id", *IDENTITY, *EXACT_FIELDS)
# measure C: the fields each level leaves out; level 1 is the strictest
LEVELS: dict[int, tuple[str, ...]] = {
    1: (),
    2: ("measurement_id", "source_row_id"),
    3: ("measurement_id", "source_row_id", "aggregation"),
}

# G4, the positive control: the reference interpreter with the constructs the engine adds for
# Kempower that write a scored field, a column's own aggregation and the positional row id.
# Each patch must apply exactly once, so a changed reference cannot pass silently.
POSITIVE_PATCHES: tuple[tuple[str, str], ...] = (
    (
        '                        column["unit"],\n'
        "                        value,\n"
        "                        default_aggregation,\n",
        '                        column["unit"],\n'
        "                        value,\n"
        '                        column.get("aggregation", default_aggregation),\n',
    ),
    (
        "        raw_row_id = record.get(row_id_field)\n",
        "        raw_row_id = (\n"
        '            record.get("_payload_position")\n'
        '            if record_cfg.get("row_id_from") == "payload_position"\n'
        "            else record.get(row_id_field)\n"
        "        )\n",
    ),
)
# G5, the negative control: the positive control with the voltage range rule ignored
NEGATIVE_PATCH = (
    "            for rule in quantity_rules.get(quantity, []):\n",
    '            for rule in [] if quantity == "voltage" else quantity_rules.get(quantity, []):\n',
)


def sha256_text_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patched(source: str, patches: tuple[tuple[str, str], ...]) -> str:
    for old, new in patches:
        if source.count(old) != 1:
            raise ValueError("a control patch no longer applies exactly once to the reference")
        source = source.replace(old, new)
    return source


def positive_control(reference: str) -> str:
    return patched(reference, POSITIVE_PATCHES)


def negative_control(reference: str) -> str:
    return patched(positive_control(reference), (NEGATIVE_PATCH,))


# ------------------------------------------------------------------ measures beyond the scorer


def _hashable(value: Any) -> Any:
    try:
        hash(value)
    except TypeError:
        return ("unhashable", repr(value))
    return value


def well_formed_rows(output: Any) -> list[dict[str, Any]] | None:
    """The output's rows when every one carries the identity fields and a value, else None."""
    if not isinstance(output, dict) or not isinstance(output.get("rows"), list):
        return None
    rows: list[dict[str, Any]] = output["rows"]
    if not all(isinstance(r, dict) and all(n in r for n in (*IDENTITY, "value")) for r in rows):
        return None
    return rows


def projected_match(
    oracle_rows: list[dict[str, Any]], output: Any, left_out: tuple[str, ...]
) -> bool:
    """The same multiset of rows once `left_out` is set aside, values within the tolerance."""
    rows = well_formed_rows(output)
    if rows is None or len(rows) != len(oracle_rows):
        return False
    fields = [name for name in SCORED_FIELDS if name not in left_out]

    def buckets(items: list[dict[str, Any]]) -> dict[tuple[Any, ...], list[Any]]:
        grouped: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
        for row in items:
            grouped[tuple(_hashable(row.get(name)) for name in fields)].append(row["value"])
        return grouped

    expected, produced = buckets(oracle_rows), buckets(rows)
    if expected.keys() != produced.keys():
        return False
    for key, values in expected.items():
        mine = produced[key]
        if len(mine) != len(values) or not all(
            isinstance(v, int | float) and not isinstance(v, bool) for v in mine
        ):
            return False
        if not all(values_close(e, p) for e, p in zip(sorted(values), sorted(mine), strict=True)):
            return False
    return True


def identity_counts(output: Any) -> dict[str, int]:
    """Measure D: rows written, and distinct `measurement_id` values among them."""
    rows = output.get("rows") if isinstance(output, dict) else None
    if not isinstance(rows, list):
        return {"rows": 0, "distinct_ids": 0}
    ids = {_hashable(r.get("measurement_id")) for r in rows if isinstance(r, dict)}
    return {"rows": len(rows), "distinct_ids": len(ids)}


def beyond_contract(
    engine_rows: list[dict[str, Any]],
    reference_rows: list[dict[str, Any]] | None,
    output: Any,
) -> dict[str, int] | None:
    """Measure E: rows that agree with the engine where the reference writes otherwise.

    Counted for two fields: an aggregation the engine writes for a quantity and the reference
    does not, and a row id the engine writes when the reference writes none.
    """
    if reference_rows is None:
        return None
    rows = well_formed_rows(output) or []
    engine_aggregations: dict[Any, set[Any]] = defaultdict(set)
    reference_aggregations: dict[Any, set[Any]] = defaultdict(set)
    for row in engine_rows:
        engine_aggregations[row["quantity"]].add(row["aggregation"])
    for row in reference_rows:
        reference_aggregations[row["quantity"]].add(row["aggregation"])
    aggregation = sum(
        1
        for row in rows
        if _hashable(row.get("aggregation"))
        in engine_aggregations[_hashable(row.get("quantity"))]
        - reference_aggregations[_hashable(row.get("quantity"))]
    )
    engine_row_ids = {row["source_row_id"] for row in engine_rows} - {None}
    row_id = 0
    if all(row["source_row_id"] is None for row in reference_rows):
        row_id = sum(1 for row in rows if _hashable(row.get("source_row_id")) in engine_row_ids)
    return {"aggregation": aggregation, "source_row_id": row_id}


def ordered_differences(
    expected_rows: list[dict[str, Any]], produced_rows: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Field by field, row by row in production order; None when the counts differ.

    Used for the reference against the engine (P2): both write rows record by record and
    column by column, so the i-th rows describe the same reading.
    """
    if len(expected_rows) != len(produced_rows):
        return None
    fields: Counter[str] = Counter()
    aggregation_by_quantity: Counter[str] = Counter()
    for expected, produced in zip(expected_rows, produced_rows, strict=True):
        for name in SCORED_FIELDS:
            if expected.get(name) != produced.get(name):
                fields[name] += 1
                if name == "aggregation":
                    aggregation_by_quantity[str(expected["quantity"])] += 1
        if not values_close(expected["value"], produced.get("value")):
            fields["value"] += 1
    return {"fields": dict(fields), "aggregation_by_quantity": dict(aggregation_by_quantity)}


def contract_exact(score: dict[str, Any]) -> bool:
    """Measure B for one input: rows, statistics and identity hashes as the reference's."""
    return bool(
        score["passed"] and score["stats_equal"] and score["id_matches"] == score["exact_rows"]
    )


# -------------------------------------------------------------------- running and summarising


def replay_program(
    source: str | None,
    mode: str,
    cases: list[Case],
    reference_outputs: dict[str, Any],
    timeout_s: float,
) -> list[dict[str, Any]]:
    """Run one program on every case and measure its output against both oracles."""
    results = []
    for case in cases:
        if source is None:
            status, output, elapsed, detail, stderr = "not_run", None, 0.0, "", ""
            violations: list[str] = []
        else:
            execution = execute(source, case.payload(mode), timeout_s)
            status, elapsed = execution.status, execution.elapsed_s
            output = execution.output if status == "ok" else None
            detail, violations, stderr = execution.detail, execution.violations, execution.stderr
        engine = score_case(case.expected_rows, case.expected_stats, output).to_dict()
        reference = reference_outputs.get(case.case_id)
        reference_rows = well_formed_rows(reference)
        reference_stats = reference.get("stats") if isinstance(reference, dict) else None
        contract = (
            score_case(reference_rows, reference_stats, output).to_dict()
            if reference_rows is not None and isinstance(reference_stats, dict)
            else None
        )
        rows = well_formed_rows(output) or []
        results.append(
            {
                "case": case.case_id,
                "status": status,
                "elapsed_s": round(elapsed, 3),
                "detail": detail,
                "violations": violations,
                "stderr_tail": stderr[-800:],
                "engine": engine,
                "contract": contract,
                "levels": {
                    str(level): engine["stats_equal"]
                    and projected_match(case.expected_rows, output, left_out)
                    for level, left_out in LEVELS.items()
                },
                "identity": identity_counts(output),
                "beyond_contract": beyond_contract(case.expected_rows, reference_rows, output),
                "session_fields": sum(1 for row in rows if row.get("session_id") is not None),
                "output": output,
            }
        )
    return results


def summarise_program(results: list[dict[str, Any]]) -> dict[str, Any]:
    """One program's outcome over all three inputs, as the protocol's measures define it."""
    engine = [r["engine"] for r in results]
    contract = [r["contract"] for r in results]
    known = all(score is not None for score in contract)
    reached = [level for level in sorted(LEVELS) if all(r["levels"][str(level)] for r in results)]
    beyond = [r["beyond_contract"] for r in results]
    return {
        "runs": all(r["status"] == "ok" for r in results),
        "engine_pass": all(s["passed"] for s in engine),
        "engine_pass_on_any_input": any(s["passed"] for s in engine),
        "engine_lenient_pass": all(
            r["status"] == "ok"
            and s["malformed_rows"] == 0
            and s["lenient_rows"] == s["oracle_rows"] == s["script_rows"]
            for r, s in zip(results, engine, strict=True)
        ),
        "engine_stats_exact": all(s["stats_equal"] for s in engine),
        "engine_identity": all(s["passed"] and s["id_matches"] == s["exact_rows"] for s in engine),
        "engine_micro": micro(engine),
        "contract_exact": all(contract_exact(s) for s in contract) if known else None,
        "contract_micro": micro(s for s in contract if s is not None) if known else None,
        "contract_rows_matched": sum(s["exact_rows"] for s in contract if s is not None),
        # the strictest level reached on all three inputs (PROTOCOL.md, deviation D1)
        "level": reached[0] if reached else None,
        "max_distinct_ids": max(r["identity"]["distinct_ids"] for r in results),
        "beyond_contract": {
            name: sum(b[name] for b in beyond if b is not None)
            for name in ("aggregation", "source_row_id")
        }
        if all(b is not None for b in beyond)
        else None,
        "session_fields": sum(r["session_fields"] for r in results),
        "statuses": dict(Counter(r["status"] for r in results)),
    }


# ------------------------------------------------------------------------------------- gates


def engine_reproduces(metadata_root: Path, cases: list[Case]) -> list[str]:
    """G2: the September cases whose frozen output the current engine does not reproduce."""

    def canonical(rows: list[dict[str, Any]]) -> list[str]:
        return sorted(json.dumps(row, sort_keys=True) for row in rows)

    different = []
    for case in cases:
        rows, stats = run_engine(metadata_root, case.rulebook, case.records, case.device_factors)
        if (canonical(rows), stats) != (canonical(case.expected_rows), case.expected_stats):
            different.append(case.case_id)
    return different


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )


def frozen_sources_unchanged(
    repo: Path, metadata_root: Path, contract: Path, reference: Path
) -> list[str]:
    """G6: every way in which something the protocol froze has moved; empty when none has."""
    problems = []
    for root, commit, paths in (
        (repo, ENGINE_COMMIT, ("src",)),
        (metadata_root, METADATA_COMMIT, FROZEN_METADATA_PATHS),
    ):
        diff = _git(root, "diff", "--stat", commit, "--", *paths)
        status = _git(root, "status", "--porcelain", "--", *paths)
        if diff.returncode or status.returncode:
            problems.append(f"{root.name}: git failed: {diff.stderr.strip()} {status.stderr}")
        elif diff.stdout.strip() or status.stdout.strip():
            problems.append(f"{root.name}: {', '.join(paths)} differ from {commit} or are dirty")
    if sha256_text_file(contract) != CONTRACT_SHA256:
        problems.append("CONTRACT.md differs from the frozen contract")
    if sha256_text_file(reference) != REFERENCE_SHA256:
        problems.append("reference/interpreter.py differs from the frozen reference")
    return problems


class NetworkRefused(OSError):
    """Raised by any connection attempt while the replay runs."""


@contextmanager
def no_network() -> Iterator[None]:
    """G7: refuse every connection this process tries to open, and restore on exit.

    Programs run in separate interpreters that the sandbox already denies network modules;
    this covers the harness process itself.
    """

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise NetworkRefused("the Kempower replay makes no network connection")

    saved = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection)
    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = saved  # type: ignore[method-assign]


def network_is_refused() -> bool:
    """Probe G7 from inside `no_network`: a connection attempt must be refused."""
    try:
        socket.create_connection(("127.0.0.1", 9), timeout=1)
    except NetworkRefused:
        return True
    except OSError:
        return False
    return False


def stats_of(output: Any) -> dict[str, Any]:
    stats = output.get("stats") if isinstance(output, dict) else None
    return {key: stats.get(key) for key in STATS_KEYS} if isinstance(stats, dict) else {}
