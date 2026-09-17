"""Turn raw results into the tables the experiment reports."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from codegen.metrics import pass_at_k, safe_mean, safe_median

_HEADLINE = (
    "| Mode | Rulebook shown | Model | n | Code | Runs | pass@1 | pass@n "
    "| pass@1 after repair | Row F1 | Row F1 after repair | Pass, timestamps as instants "
    "| Stats exact | Identity hash | Drift survival | Median gen s |"
)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _rate(flags: list[bool | None]) -> float | None:
    known = [flag for flag in flags if flag is not None]
    return sum(1 for flag in known if flag) / len(known) if known else None


def _headline_row(mode: str, seen: str, model: str, samples: list[dict[str, Any]]) -> str:
    n = len(samples)
    first = [s["attempts"][0] for s in samples]
    final = [s["attempts"][-1] for s in samples]
    passed_first = sum(1 for a in first if a["summary"]["test_pass"])
    passed_final = sum(1 for a in final if a["summary"]["test_pass"])
    drift = safe_mean(
        a["summary"]["drift_survival"] for a in final if a["summary"]["drift_survival"] is not None
    )
    median_s = safe_median(a["generation"]["elapsed_s"] for a in first) or 0.0
    cells = [
        mode,
        seen,
        f"`{model}`",
        str(n),
        _pct(_rate([a["extracted"] for a in first])),
        _pct(_rate([a["summary"]["runs"] for a in first])),
        _pct(pass_at_k(n, passed_first, 1)),
        _pct(pass_at_k(n, passed_first, n)),
        _pct(pass_at_k(n, passed_final, 1)),
        _pct(safe_mean(a["summary"]["test_micro"]["f1"] for a in first)),
        _pct(safe_mean(a["summary"]["test_micro"]["f1"] for a in final)),
        _pct(_rate([a["summary"]["lenient_pass"] for a in first])),
        _pct(_rate([a["summary"]["stats_pass"] for a in first])),
        _pct(_rate([a["summary"]["id_pass"] for a in first])),
        _pct(drift),
        f"{median_s:.0f}",
    ]
    return "| " + " | ".join(cells) + " |"


def _failure_line(mode: str, seen: str, model: str, samples: list[dict[str, Any]]) -> str:
    statuses: Counter[str] = Counter()
    fields: Counter[str] = Counter()
    near: Counter[str] = Counter()
    stats_keys: Counter[str] = Counter()
    missing = 0
    extra = 0
    cut_off = 0
    resent = 0
    for sample in samples:
        attempt = sample["attempts"][0]
        if attempt["generation"].get("finish_reason") == "length":
            cut_off += 1
        resent += int(attempt["generation"].get("truncation_retries") or 0)
        for case in attempt["cases"]:
            if case["split"] != "test":
                continue
            statuses[case["status"]] += 1
            # a row whose identity differs is missing and extra at once, and is named by no field
            missing += int(case["score"].get("missing") or 0)
            extra += int(case["score"].get("extra") or 0)
            fields.update(case["score"]["field_errors"])
            near.update(case["score"]["near_miss_fields"])
            stats_keys.update(case["score"]["stats_diff"].keys())  # values are [expected, produced]
    return (
        f"- **{mode}, {seen}, `{model}`**: test executions {dict(statuses)}; "
        f"rows missing {missing}, extra {extra}; "
        f"wrong fields {dict(fields) or 'none'}; wrong identity fields {dict(near) or 'none'}; "
        f"statistics that differ {dict(stats_keys) or 'none'}; "
        f"replies cut off by the token limit {cut_off}; "
        f"prompts resent because the provider truncated them {resent}."
    )


def build_report(raw: dict[str, Any]) -> str:
    meta = raw.get("meta", {})
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for sample in raw.get("samples", []):
        groups[(sample["mode"], sample["seen_vendor"], sample["model"])].append(sample)

    lines = [
        "# Can a language model write the transformation code?",
        "",
        f"Engine `{meta.get('engine_commit', '?')}`, "
        f"rulebook `{meta.get('metadata_commit', '?')}`, "
        f"contract `{str(meta.get('contract_sha256', '?'))[:12]}`. "
        f"Temperature {meta.get('temperature')}, {meta.get('n')} samples per cell, "
        f"up to {meta.get('repair')} repair attempt(s) using synthetic example feedback only.",
        "",
        "Scores are on held-out test inputs the model never saw, including real landing-zone "
        "records. **pass** means every row and every field equals the engine's output on every "
        "test input.",
        "",
        _HEADLINE,
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [_headline_row(*key, samples) for key, samples in sorted(groups.items())]

    interpreter = {key: value for key, value in groups.items() if key[0] == "interpreter"}
    if interpreter:
        lines += [
            "",
            "## Interpreter programs on a vendor they were not shown",
            "",
            "| Rulebook shown | Model | Pass on shown vendor | Pass on other vendor "
            "| Drift survival |",
            "|---|---|---|---|---|",
        ]
        for (_, seen, model), samples in sorted(interpreter.items()):
            final = [s["attempts"][-1]["summary"] for s in samples]
            drift = safe_mean(f["drift_survival"] for f in final if f["drift_survival"] is not None)
            lines.append(
                f"| {seen} | `{model}` | {_pct(_rate([f['test_pass'] for f in final]))} "
                f"| {_pct(_rate([f['unseen_test_pass'] for f in final]))} "
                f"| {_pct(drift)} |"
            )

    lines += ["", "## What went wrong", ""]
    lines += [_failure_line(*key, samples) for key, samples in sorted(groups.items())]
    return "\n".join(lines) + "\n"
