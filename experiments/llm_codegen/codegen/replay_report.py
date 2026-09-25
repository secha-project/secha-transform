"""The Kempower replay's report, and its registered predictions checked mechanically.

Each prediction of kempower_replay/PROTOCOL.md is evaluated by code written before the replay
ran, so whether it held is read from the results, not decided after seeing them. The report
stays with the results, which are gitignored, because a program's failure message may quote a
value from a record; what goes into findings.md is counts, digests and field names.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

GAP_FIELDS = frozenset({"source_row_id", "measurement_id", "aggregation"})
OWN_AGGREGATION = frozenset({"state_of_charge", "temperature"})
SHOWN = {"mx_electrix": "MX Electrix", "procem_kampusareena_pq": "ProCem"}


def _interpreters(result: dict[str, Any], run: str) -> list[dict[str, Any]]:
    return [p for p in result["programs"] if p["run"] == run and p["mode"] == "interpreter"]


def _exact_count(programs: list[dict[str, Any]]) -> int:
    return sum(1 for p in programs if p["summary"]["contract_exact"])


def evaluate_predictions(result: dict[str, Any]) -> list[dict[str, Any]]:
    """P1 to P5, each with whether it held and the evidence it was judged on."""
    reference = result["reference"]
    programs = result["programs"]
    outcomes = []

    passing = [p["key"] for p in programs if p["summary"]["engine_pass_on_any_input"]]
    if reference["summary"]["engine_pass_on_any_input"]:
        passing.append("reference")
    outcomes.append(
        {
            "id": "P1",
            "holds": not passing,
            "evidence": "nothing passes against the engine on any input"
            if not passing
            else f"passes against the engine: {', '.join(passing)}",
        }
    )

    checks = []
    union: set[str] = set()
    for case in reference["cases"]:
        diff = case["differences"]
        if diff is None:
            checks.append(f"{case['case']}: row counts differ from the engine's")
            continue
        fields = set(diff["fields"])
        union |= fields
        if not fields <= GAP_FIELDS:
            checks.append(f"{case['case']}: also differs in {sorted(fields - GAP_FIELDS)}")
        for name in ("source_row_id", "measurement_id"):
            if diff["fields"].get(name, 0) != case["engine_rows"]:
                checks.append(f"{case['case']}: {name} differs on {diff['fields'].get(name, 0)}")
        if diff["aggregation_by_quantity"] != case["own_aggregation_rows"]:
            checks.append(
                f"{case['case']}: aggregation differs by quantity as "
                f"{diff['aggregation_by_quantity']}, not {case['own_aggregation_rows']}"
            )
        if not case["stats_equal"]:
            checks.append(f"{case['case']}: statistics differ from the engine's")
    golden = next(c for c in reference["cases"] if c["case"].endswith("golden_edge"))
    golden_aggregation = sum(
        (golden["differences"] or {}).get("aggregation_by_quantity", {}).values()
    )
    if (golden_aggregation, golden["engine_rows"]) != (9, 24):
        checks.append(
            f"golden_edge: aggregation differs on {golden_aggregation} of {golden['engine_rows']}"
        )
    if union != GAP_FIELDS:
        checks.append(f"the fields that differ are {sorted(union)}")
    if not reference["summary"]["runs"]:
        checks.append(
            f"the reference did not run on every input: {reference['summary']['statuses']}"
        )
    if reference["summary"]["level"] != 3:
        checks.append(f"the reference reaches level {reference['summary']['level']}, not 3")
    outcomes.append(
        {
            "id": "P2",
            "holds": not checks,
            "evidence": "; ".join(checks)
            or "exactly source_row_id, measurement_id and the aggregation of state of charge "
            "and temperature differ; statistics equal; level 3",
        }
    )

    collapse = []
    for case in reference["cases"]:
        if not (case["distinct_ids"] == case["quantities"] <= 5):
            collapse.append(
                f"{case['case']}: {case['distinct_ids']} ids for {case['quantities']} quantities"
            )
        if case["engine_distinct_ids"] != case["engine_rows"]:
            collapse.append(f"{case['case']}: the engine repeats an id")
    outcomes.append(
        {
            "id": "P3",
            "holds": not collapse,
            "evidence": "; ".join(collapse)
            or ", ".join(
                f"{c['case'].rsplit('/', 1)[-1]} {c['distinct_ids']} ids for "
                f"{c['engine_rows']} rows"
                for c in reference["cases"]
            ),
        }
    )

    counts = {
        run: _exact_count(_interpreters(result, run))
        for run in ("kimi-k3", "codestral-2508", "phi4-14b")
    }
    by_shown = {
        run: {
            shown: _exact_count(
                [p for p in _interpreters(result, run) if p["seen_vendor"] == shown]
            )
            for shown in SHOWN
        }
        for run in ("kimi-k3", "codestral-2508")
    }
    within = (
        counts["kimi-k3"] >= 7 and 2 <= counts["codestral-2508"] <= 5 and counts["phi4-14b"] <= 1
    )
    wide_first = all(
        shown["mx_electrix"] >= shown["procem_kampusareena_pq"] for shown in by_shown.values()
    )
    outcomes.append(
        {
            "id": "P4",
            "holds": within and wide_first,
            "evidence": (
                f"contract-exact: kimi-k3 {counts['kimi-k3']}/10, codestral-2508 "
                f"{counts['codestral-2508']}/10, phi4-14b {counts['phi4-14b']}/10; shown MX "
                "Electrix against ProCem: "
                + ", ".join(
                    f"{run} {s['mx_electrix']} against {s['procem_kampusareena_pq']}"
                    for run, s in by_shown.items()
                )
            ),
        }
    )

    floor = [p for p in programs if p["role"] == "floor"]
    matching = [p["key"] for p in floor if p["summary"]["contract_rows_matched"]]
    unknown = [p["key"] for p in floor if p["summary"]["contract_exact"] is None]
    outcomes.append(
        {
            "id": "P5",
            "holds": None if unknown else not matching,
            "evidence": "the reference did not run on every input, so P5 cannot be judged"
            if unknown
            else (
                f"none of the {len(floor)} snapshot programs writes a row of the reference's output"
                if not matching
                else f"rows equal to the reference's from {', '.join(matching)}"
            ),
        }
    )
    return outcomes


def _yes(value: Any) -> str:
    return "n/a" if value is None else ("yes" if value else "no")


def build_replay_report(result: dict[str, Any]) -> str:
    meta = result["meta"]
    frozen = meta["frozen"]["engine_commit"]
    contract = meta["contract_sha256"][:12]
    lines = [
        "# Kempower replay: report",
        "",
        f"Run {meta['started_at']} to {meta['finished_at']}. Replay code at "
        f"`{meta['engine_commit']}` (engine source frozen at `{frozen}`), rulebook "
        f"`{meta['metadata_commit']}`, contract `{contract}`. "
        "Protocol: `kempower_replay/PROTOCOL.md`.",
    ]
    if meta.get("rerun_reason"):
        lines += ["", f"This is a second run: {meta['rerun_reason']}"]

    lines += ["", "## Gates", "", "| Gate | Passed | Detail |", "|---|---|---|"]
    lines += [f"| {g['gate']} | {_yes(g['passed'])} | {g['detail']} |" for g in result["gates"]]

    lines += [
        "",
        "## Cases",
        "",
        "| Case | Records | Engine rows | Suspect | Rejected | Null cells | Sessions | Input |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for case in result["cases"]:
        s = case["stats"]
        lines.append(
            f"| {case['case']} | {case['records']} | {case['rows']} | {s['rows_suspect']} | "
            f"{s['records_rejected']} | {s['cells_null_skipped']} | "
            f"{case['sessions_full_rulebook']} | `{case['input_sha256'][:12]}` |"
        )

    lines += ["", "## Predictions", "", "| Prediction | Held | Evidence |", "|---|---|---|"]
    lines += [
        f"| {p['id']} | {_yes(p['holds'])} | {p['evidence']} |"
        for p in evaluate_predictions(result)
    ]

    reference = result["reference"]
    lines += [
        "",
        "## The reference interpreter (the contract's ceiling)",
        "",
        f"Level {reference['summary']['level']}; statuses {reference['summary']['statuses']}.",
        "",
        "| Case | Rows | Distinct ids | Quantities | Fields that differ from the engine |",
        "|---|---|---|---|---|",
    ]
    for case in reference["cases"]:
        diff = case["differences"]
        described = (
            "row counts differ"
            if diff is None
            else ", ".join(f"{name} {count}" for name, count in sorted(diff["fields"].items()))
            or "none"
        )
        lines.append(
            f"| {case['case']} | {case['rows']} | {case['distinct_ids']} | "
            f"{case['quantities']} | {described} |"
        )

    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for program in result["programs"]:
        key = (program["run"], program["role"], program["mode"], program["seen_vendor"])
        groups[key].append(program)
    lines += [
        "",
        "## Arms",
        "",
        "| Model | Role | Mode | Shown | Programs | Run on all inputs | Pass against engine | "
        "Contract-exact | Level 1 / 2 / 3 / none | Most distinct ids | Beyond the contract |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (run, role, mode, shown), members in sorted(groups.items()):
        summaries = [m["summary"] for m in members]
        levels = Counter(s["level"] for s in summaries)
        beyond = sum(
            1 for s in summaries if s["beyond_contract"] and any(s["beyond_contract"].values())
        )
        lines.append(
            f"| {run} | {role} | {mode} | {SHOWN[shown]} | {len(members)} | "
            f"{sum(s['runs'] for s in summaries)} | {sum(s['engine_pass'] for s in summaries)} | "
            f"{sum(bool(s['contract_exact']) for s in summaries)} | "
            f"{levels[1]} / {levels[2]} / {levels[3]} / {levels[None]} | "
            f"{max(s['max_distinct_ids'] for s in summaries)} | {beyond} |"
        )

    lines += [
        "",
        "## Programs",
        "",
        "| Program | Attempt | Statuses | Engine | Contract-exact | Level | Ids | Beyond | "
        "First failure |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for program in result["programs"]:
        s = program["summary"]
        failing = next((r for r in program["results"] if r["status"] != "ok"), None)
        first = (
            ""
            if failing is None
            else f"{failing['case'].rsplit('/', 1)[-1]}: {failing['status']} "
            f"{(failing['stderr_tail'].strip().splitlines() or [failing['detail']])[-1][:90]}"
        )
        lines.append(
            f"| {program['key']} | {program['attempt']} | {s['statuses']} | "
            f"{_yes(s['engine_pass'])} | {_yes(s['contract_exact'])} | {s['level']} | "
            f"{s['max_distinct_ids']} | {s['beyond_contract']} | {first} |"
        )
    return "\n".join(lines) + "\n"
