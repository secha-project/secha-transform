"""Can a language model write the transformation code the engine makes unnecessary?

A generated program receives exactly what the engine receives and is scored row by row
against the engine's output. The engine is the oracle, so no answer key is written by hand.

Usage:
    python harness.py build-cases            # freeze inputs and the engine's output for them
    python harness.py selfcheck              # prove the harness gives a correct program 100%
    python harness.py dry-run --mode snapshot --vendor mx_electrix
    python harness.py probe-context --model phi4-14b --tokens 12000
    python harness.py run --models phi4-14b codestral-2508 --n 5 --repair 1
    python harness.py report --results results/<name>
    python harness.py inventory              # check the Kempower replay's frozen programs
    python harness.py replay-cases           # build the replay's three Kempower cases
    python harness.py replay --gates-only    # the gates G1 to G7, without the replay
    python harness.py replay                 # the gates, then the replay, once
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import platform
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codegen.cases import MODES, VENDORS, Case, build_cases, load_cases, save_cases
from codegen.client import ChatClient, endpoint_for, extract_code, load_env_file, provider_tag, slug
from codegen.compare import score_case
from codegen.metrics import micro
from codegen.prompts import feedback_for, interpreter_messages, repair_messages, snapshot_messages
from codegen.replay import build_inventory, digest, inventory_differences
from codegen.replay_cases import build_replay_cases, records_digest
from codegen.replay_report import OWN_AGGREGATION, build_replay_report
from codegen.replay_run import (
    ENGINE_COMMIT,
    METADATA_COMMIT,
    engine_reproduces,
    frozen_sources_unchanged,
    negative_control,
    network_is_refused,
    no_network,
    ordered_differences,
    positive_control,
    replay_program,
    summarise_program,
    well_formed_rows,
)
from codegen.report import build_report
from codegen.sandbox import execute

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CONTRACT = HERE / "CONTRACT.md"
REFERENCE = HERE / "reference" / "interpreter.py"
REPLAY = HERE / "kempower_replay"
FREEZE_LINE = "RULEBOOK: dict[str, Any] | None = None"
MUTANTS = {
    "no_scaling": (
        "return None if factor is None else float(raw) * factor",
        "return None if factor is None else float(raw)",
    ),
    "no_utc_suffix": ('return text if parsed.tzinfo is not None else text + "Z"', "return text"),
}


def _git(path: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _provenance(metadata_root: Path) -> dict[str, Any]:
    return {
        "engine_commit": _git(REPO, "rev-parse", "--short", "HEAD"),
        "engine_dirty": bool(_git(REPO, "status", "--porcelain", "--", "src")),
        "metadata_commit": _git(metadata_root, "rev-parse", "--short", "HEAD"),
        "metadata_dirty": bool(
            _git(metadata_root, "status", "--porcelain", "--", "canonical", "vendors", "targets")
        ),
        "contract_sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def freeze(source: str, rulebook: dict[str, Any]) -> str:
    """The reference interpreter with one vendor's rulebook written into it."""
    if source.count(FREEZE_LINE) != 1:
        raise ValueError("the reference interpreter no longer has exactly one freeze line")
    return source.replace(
        FREEZE_LINE, f"RULEBOOK: dict[str, Any] | None = json.loads({json.dumps(rulebook)!r})"
    )


def evaluate(
    source: str | None, cases: list[Case], mode: str, timeout_s: float
) -> list[dict[str, Any]]:
    """Run a program on every case and score each output against the engine's."""
    results = []
    for case in cases:
        if source is None:
            status, output, elapsed, detail, violations, stderr = "not_run", None, 0.0, "", [], ""
        else:
            execution = execute(source, case.payload(mode), timeout_s)
            status, elapsed = execution.status, execution.elapsed_s
            output = execution.output if status == "ok" else None
            detail, violations, stderr = execution.detail, execution.violations, execution.stderr
        score = score_case(case.expected_rows, case.expected_stats, output)
        results.append(
            {
                "case": case.case_id,
                "vendor": case.vendor,
                "split": case.split,
                "name": case.name,
                "mutation": case.mutation,
                "status": status,
                "elapsed_s": round(elapsed, 3),
                "detail": detail,
                "violations": violations,
                "stderr_tail": stderr[-800:],
                "score": score.to_dict(),
            }
        )
    return results


def summarise(
    dev: dict[str, Any], results: list[dict[str, Any]], seen_vendor: str
) -> dict[str, Any]:
    test = [r for r in results if r["split"] == "test"]
    drift = [r for r in results if r["split"] == "drift"]
    seen = [r for r in test if r["vendor"] == seen_vendor]
    unseen = [r for r in test if r["vendor"] != seen_vendor]

    def all_pass(items: list[dict[str, Any]]) -> bool:
        return bool(items) and all(r["score"]["passed"] for r in items)

    return {
        "dev_pass": dev["score"]["passed"] and dev["score"]["stats_equal"],
        "runs": bool(seen) and all(r["status"] == "ok" for r in seen),
        "test_pass": all_pass(seen),
        "unseen_test_pass": all_pass(unseen) if unseen else None,
        "lenient_pass": bool(seen)
        and all(
            r["status"] == "ok"
            and r["score"]["malformed_rows"] == 0
            and r["score"]["lenient_rows"] == r["score"]["oracle_rows"] == r["score"]["script_rows"]
            for r in seen
        ),
        "stats_pass": bool(seen) and all(r["score"]["stats_equal"] for r in seen),
        "id_pass": all_pass(seen)
        and all(r["score"]["id_matches"] == r["score"]["exact_rows"] for r in seen),
        "test_micro": micro(r["score"] for r in seen),
        "drift_survival": sum(1 for r in drift if r["score"]["passed"]) / len(drift)
        if drift
        else None,
        "statuses": dict(Counter(r["status"] for r in results)),
    }


# ------------------------------------------------------------------------------------ commands


def cmd_build_cases(args: argparse.Namespace) -> int:
    landing = args.landing_root if args.landing_root.exists() else None
    if landing is None:
        print(f"landing zone not found at {args.landing_root}: building synthetic cases only")
    cases = build_cases(args.metadata_root, landing)
    manifest = _provenance(args.metadata_root) | {
        "landing_root": str(landing) if landing else None,
        "cases": [
            {
                "case": c.case_id,
                "records": len(c.records),
                "rows": len(c.expected_rows),
                "real_data": c.real_data,
                "stats": c.expected_stats,
            }
            for c in cases
        ],
    }
    save_cases(cases, args.cases, manifest)
    print(f"{'case':<58} {'records':>7} {'rows':>6}  stats")
    for c in cases:
        s = c.expected_stats
        print(
            f"{c.case_id:<58} {len(c.records):>7} {len(c.expected_rows):>6}  "
            f"rejected={s['records_rejected']} unmapped={s['records_unmapped']} "
            f"suspect={s['rows_suspect']} "
            f"null={s['cells_null_skipped']}"
        )
    print(f"\n{len(cases)} cases written to {args.cases}")
    return 0


def cmd_selfcheck(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    reference = REFERENCE.read_text(encoding="utf-8")
    failures: list[str] = []

    for r in evaluate(reference, cases, "interpreter", args.exec_timeout):
        s = r["score"]
        if not (
            r["status"] == "ok"
            and s["passed"]
            and s["stats_equal"]
            and s["id_matches"] == s["exact_rows"]
        ):
            failures.append(
                f"reference interpreter fails {r['case']}: {r['status']} {r['detail']} "
                f"{r['stderr_tail'][-300:]} {s}"
            )
    verdict = "all exact" if not failures else f"{len(failures)} failing"
    print(f"reference interpreter: {len(cases)} cases, {verdict}")

    for vendor in VENDORS:
        own = [c for c in cases if c.vendor == vendor and c.split in {"dev", "test"}]
        drift = [c for c in cases if c.vendor == vendor and c.split == "drift"]
        frozen = freeze(reference, next(c for c in own if c.split == "dev").rulebook)
        own_results = evaluate(frozen, own, "snapshot", args.exec_timeout)
        drift_results = evaluate(frozen, drift, "snapshot", args.exec_timeout)
        own_pass = all(r["score"]["passed"] and r["score"]["stats_equal"] for r in own_results)
        survived = [r["case"] for r in drift_results if r["score"]["passed"]]
        if not own_pass:
            failures.append(f"frozen reference for {vendor} fails its own cases")
        if survived:
            failures.append(f"drift cases did not detect a frozen rulebook: {survived}")
        print(
            f"frozen reference, {vendor}: own cases {'exact' if own_pass else 'FAILING'}, "
            f"drift caught {len(drift) - len(survived)} of {len(drift)}"
        )

    scored = [c for c in cases if c.split in {"dev", "test"}]
    for name, (old, new) in MUTANTS.items():
        if reference.count(old) != 1:
            failures.append(f"mutant {name} no longer applies to the reference")
            continue
        results = evaluate(reference.replace(old, new), scored, "interpreter", args.exec_timeout)
        caught = [r["case"] for r in results if not r["score"]["passed"]]
        if not caught:
            failures.append(f"mutant {name} was not detected")
        if name == "no_utc_suffix":
            format_only = [
                r["case"]
                for r in results
                if not r["score"]["passed"]
                and r["score"]["lenient_rows"]
                == r["score"]["oracle_rows"]
                == r["score"]["script_rows"]
            ]
            if not format_only:
                failures.append(
                    "the lenient timestamp tier did not recognise a format-only difference"
                )
        print(f"mutant {name}: caught on {len(caught)} of {len(results)} cases")

    if failures:
        print("\nSELFCHECK FAILED")
        for failure in failures:
            print(" -", failure)
        return 1
    print(
        "\nselfcheck passed: the harness scores a correct program as exact "
        "and catches every planted defect"
    )
    return 0


def _messages(mode: str, contract: str, dev: Case) -> list[dict[str, str]]:
    return (
        snapshot_messages(contract, dev)
        if mode == "snapshot"
        else interpreter_messages(contract, dev)
    )


def cmd_dry_run(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    dev = next(c for c in cases if c.vendor == args.vendor and c.split == "dev")
    messages = _messages(args.mode, CONTRACT.read_text(encoding="utf-8"), dev)
    for message in messages:
        print(f"--- {message['role']} ---\n{message['content']}\n")
    chars = sum(len(m["content"]) for m in messages)
    print(f"({chars} characters, roughly {chars // 4} tokens)")
    return 0


def cmd_probe_context(args: argparse.Namespace) -> int:
    """Does the endpoint really read a long prompt, or silently truncate it?

    Ollama-style services often default to a small context window whatever the model supports,
    and truncation looks exactly like a model that cannot follow a long specification.
    """
    load_env_file(HERE / ".env")
    # no cache and no truncation check: this command observes truncation directly, by recall
    client = ChatClient(
        HERE / ".cache", timeout_s=args.timeout, check_truncation=False, use_cache=False
    )
    endpoint = args.endpoint or endpoint_for(args.model)
    word = "saffron" + hashlib.sha256(str(args.tokens).encode()).hexdigest()[:6]
    filler = "This sentence is filler that only exists to measure how much of a prompt is read. "
    content = (
        f"Remember this code word: {word}.\n\n"
        + filler * max(1, args.tokens // 16)
        + "\n\nWhat was the code word given at the very start? Reply with the code word only."
    )
    messages = [{"role": "user", "content": content}]
    read_in_full = 0
    for repeat in range(args.repeats):
        completion = client.complete(endpoint, args.model, messages, 0.0, 30, sample=repeat)
        if completion.text is None:
            print(f"send {repeat}: {completion.error}")
            continue
        recalled = word in completion.text
        read_in_full += recalled
        counted = completion.usage.get("prompt_tokens")
        print(f"send {repeat}: provider counted {counted} tokens, recalled the word: {recalled}")
    print(
        f"{args.model} at {provider_tag(endpoint)}: {read_in_full} of {args.repeats} sends "
        f"read the whole prompt of {len(content)} characters"
    )
    return 0 if read_in_full == args.repeats else 1


def cmd_run(args: argparse.Namespace) -> int:
    load_env_file(HERE / ".env")
    cases = load_cases(args.cases)
    contract = CONTRACT.read_text(encoding="utf-8")
    args.out.mkdir(parents=True, exist_ok=True)
    raw_path = args.out / "raw.json"
    client = ChatClient(HERE / ".cache", timeout_s=args.timeout, delay_s=args.delay)
    meta = _provenance(args.metadata_root) | {
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "n": args.n,
        "repair": args.repair,
        "models": args.models,
        "modes": args.modes,
        "vendors": args.vendors,
    }
    raw: dict[str, Any] = (
        json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else {"samples": []}
    )
    raw["meta"] = meta
    done = {(s["mode"], s["seen_vendor"], s["model"], s["sample"]) for s in raw["samples"]}

    for mode in args.modes:
        for seen in args.vendors:
            dev = next(c for c in cases if c.vendor == seen and c.split == "dev")
            targets = [
                c
                for c in cases
                if c.split in {"test", "drift"} and (mode == "interpreter" or c.vendor == seen)
            ]
            base_messages = _messages(mode, contract, dev)
            for model in args.models:
                endpoint = args.endpoint or endpoint_for(model)
                for sample in range(args.n):
                    if (mode, seen, model, sample) in done:
                        continue
                    print(f"[{mode} | {seen} | {model} | sample {sample}]", flush=True)
                    messages = base_messages
                    attempts: list[dict[str, Any]] = []
                    for attempt in range(args.repair + 1):
                        completion = client.complete(
                            endpoint, model, messages, args.temperature, args.max_tokens, sample
                        )
                        if completion.provider_failure:
                            # The provider failed, not the model. Record nothing, so running the
                            # same command again retries this sample instead of skipping it.
                            print(
                                f"   provider failure, sample not recorded: {completion.error}\n"
                                "   run the same command again to resume",
                                flush=True,
                            )
                            return 3
                        code = extract_code(completion.text)
                        script = None
                        if code is not None:
                            path = (
                                args.out
                                / "scripts"
                                / mode
                                / seen
                                / slug(model)
                                / f"s{sample}_a{attempt}.py"
                            )
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text(code, encoding="utf-8")
                            script = str(path.relative_to(args.out))
                        dev_execution = (
                            execute(code, dev.payload(mode), args.exec_timeout) if code else None
                        )
                        dev_output = (
                            dev_execution.output
                            if dev_execution and dev_execution.status == "ok"
                            else None
                        )
                        dev_score = score_case(dev.expected_rows, dev.expected_stats, dev_output)
                        dev_result = {
                            "status": dev_execution.status if dev_execution else "not_run",
                            "score": dev_score.to_dict(),
                        }
                        results = evaluate(code, targets, mode, args.exec_timeout)
                        summary = summarise(dev_result, results, seen)
                        attempts.append(
                            {
                                "attempt": attempt,
                                "generation": {
                                    "elapsed_s": round(completion.elapsed_s, 1),
                                    "cached": completion.cached,
                                    "finish_reason": completion.finish_reason,
                                    "usage": completion.usage,
                                    "truncation_retries": completion.truncation_retries,
                                    "served_model": completion.served_model,
                                    "error": completion.error,
                                },
                                "extracted": code is not None,
                                "script": script,
                                "dev": dev_result,
                                "cases": results,
                                "summary": summary,
                            }
                        )
                        print(
                            f"   attempt {attempt}: {'code' if code else 'no code'}"
                            f"{' (' + completion.error + ')' if completion.error else ''}, dev "
                            f"{'pass' if summary['dev_pass'] else 'fail'}, test "
                            f"{'PASS' if summary['test_pass'] else 'fail'} "
                            f"(row F1 {summary['test_micro']['f1']:.2f}), "
                            f"drift {summary['drift_survival']}",
                            flush=True,
                        )
                        if completion.text is None or summary["dev_pass"] or attempt == args.repair:
                            break
                        feedback = feedback_for(
                            dev, dev_execution, dev_score, dev_output, code is not None
                        )
                        messages = repair_messages(messages, completion.text, feedback)
                    raw["samples"].append(
                        {
                            "mode": mode,
                            "seen_vendor": seen,
                            "model": model,
                            "endpoint": provider_tag(endpoint),
                            "sample": sample,
                            "temperature": args.temperature,
                            "max_tokens": args.max_tokens,
                            "attempts": attempts,
                        }
                    )
                    # written after every sample, so an interrupted run loses at most one
                    raw_path.write_text(json.dumps(raw, indent=1), encoding="utf-8")

    report = build_report(raw)
    (args.out / "comparison.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """One report over one or more result directories, for example one per model run."""
    runs = [json.loads((path / "raw.json").read_text(encoding="utf-8")) for path in args.results]
    merged = {
        "meta": runs[0].get("meta", {}),
        "samples": [sample for run in runs for sample in run.get("samples", [])],
    }
    report = build_report(merged)
    destination = args.out or args.results[0] / "comparison.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(report, encoding="utf-8")
    print(report)
    return 0


def cmd_inventory(args: argparse.Namespace) -> int:
    """Check, or with --write record, the programs the Kempower replay may run."""
    current = build_inventory(HERE / "results", HERE / ".cache")
    path = REPLAY / "programs.json"
    if args.write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(f"recorded {len(current['programs'])} programs in {path.relative_to(HERE)}")
        return 0
    recorded = json.loads(path.read_text(encoding="utf-8"))
    differences = inventory_differences(recorded, current)
    for line in differences:
        print(line)
    if differences:
        return 1
    print(f"all {len(current['programs'])} programs match {path.relative_to(HERE)}")
    return 0


def cmd_replay_cases(args: argparse.Namespace) -> int:
    """Build the replay's three cases (PROTOCOL.md, Inputs). The September cases are untouched."""
    cases, facts = build_replay_cases(args.metadata_root, args.landing_root)
    manifest = _provenance(args.metadata_root) | {
        "landing_root": str(args.landing_root),
        "cases": facts,
    }
    save_cases(cases, REPLAY / "cases", manifest)
    print(f"{'case':<32} {'records':>7} {'rows':>6} {'sessions':>8}  input")
    for f in facts:
        print(
            f"{f['case']:<32} {f['records']:>7} {f['rows']:>6} "
            f"{f['sessions_full_rulebook']:>8}  {f['input_sha256'][:12]}"
        )
    print(f"\n{len(cases)} cases written to {(REPLAY / 'cases').relative_to(HERE)}")
    return 0


def _replay_gates(
    args: argparse.Namespace, cases: list[Case], inventory: dict[str, Any], reference: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """G1 to G7 (PROTOCOL.md, Validation before the replay), and the controls' results."""
    gates: list[dict[str, Any]] = []

    def gate(name: str, passed: bool, detail: str) -> None:
        gates.append({"gate": name, "passed": passed, "detail": detail})
        print(f"{name}: {'passed' if passed else 'FAILED'}: {detail}", flush=True)

    gate("G7", network_is_refused(), "a connection attempt from the harness is refused")
    gate("G1", cmd_selfcheck(args) == 0, "`python harness.py selfcheck`")

    september = load_cases(args.cases)
    different = engine_reproduces(args.metadata_root, september)
    gate(
        "G2",
        not different,
        f"{len(september) - len(different)} of {len(september)} September cases reproduced"
        + (f"; different: {', '.join(different)}" if different else ""),
    )

    differences = inventory_differences(
        inventory, build_inventory(HERE / "results", HERE / ".cache")
    )
    gate("G3", not differences, "; ".join(differences) or f"{len(inventory['programs'])} programs")

    positive = replay_program(
        positive_control(reference), "interpreter", cases, {}, args.exec_timeout
    )
    gate(
        "G4",
        all(
            r["status"] == "ok"
            and r["engine"]["passed"]
            and r["engine"]["stats_equal"]
            and r["engine"]["id_matches"] == r["engine"]["exact_rows"]
            for r in positive
        ),
        "positive control against the engine: "
        + ", ".join(f"{r['case'].rsplit('/', 1)[-1]} {r['status']}" for r in positive),
    )
    negative = replay_program(
        negative_control(reference), "interpreter", cases, {}, args.exec_timeout
    )
    golden = next(r for r in negative if r["case"].endswith("golden_edge"))
    gate(
        "G5",
        not golden["engine"]["passed"] and "quality" in golden["engine"]["field_errors"],
        f"negative control on golden_edge: field errors {golden['engine']['field_errors']}",
    )

    moved = frozen_sources_unchanged(REPO, args.metadata_root, CONTRACT, REFERENCE)
    provenance = _provenance(args.metadata_root)
    if provenance["engine_dirty"] or provenance["metadata_dirty"]:
        moved.append("a working tree is dirty")
    manifest = json.loads((REPLAY / "cases" / "manifest.json").read_text(encoding="utf-8"))
    built = {f["case"]: f["input_sha256"] for f in manifest["cases"]}
    moved += [
        f"{case.case_id} differs from the input its manifest records"
        for case in cases
        if built.get(case.case_id) != records_digest(case.records)
    ]
    gate(
        "G6",
        not moved,
        "; ".join(moved)
        or f"src as at {ENGINE_COMMIT}, rulebook as at {METADATA_COMMIT}, contract, reference "
        "and cases unchanged, trees clean",
    )
    gates.sort(key=lambda g: g["gate"])

    def without_output(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in r.items() if k != "output"} for r in results]

    return gates, {"positive": without_output(positive), "negative": without_output(negative)}


def cmd_replay(args: argparse.Namespace) -> int:
    """The gates, then every registered program and the reference on every Kempower case."""
    results_dir = REPLAY / "results"
    record = results_dir / "replay.json"
    if not args.gates_only and record.exists() and not args.rerun_reason:
        print("the replay has already run; a second run needs --rerun-reason (PROTOCOL.md)")
        return 1
    started = datetime.now(UTC).isoformat(timespec="seconds")
    cases = load_cases(REPLAY / "cases")
    inventory = json.loads((REPLAY / "programs.json").read_text(encoding="utf-8"))
    reference = REFERENCE.read_text(encoding="utf-8")
    results_dir.mkdir(parents=True, exist_ok=True)

    with no_network():
        gates, controls = _replay_gates(args, cases, inventory, reference)
        passed = all(g["passed"] for g in gates)
        (results_dir / "gates.json").write_text(
            json.dumps({"checked_at": started, "gates": gates, "controls": controls}, indent=1),
            encoding="utf-8",
        )
        if args.gates_only or not passed:
            print("\nall gates passed" if passed else "\nA GATE FAILED: the replay does not start")
            return 0 if passed else 1

        print("\nreplaying the reference interpreter and 60 programs", flush=True)
        outputs: dict[str, dict[str, Any]] = {}
        reference_results = replay_program(reference, "interpreter", cases, {}, args.exec_timeout)
        reference_outputs = {r["case"]: r["output"] for r in reference_results}
        reference_cases = []
        for case, result in zip(cases, reference_results, strict=True):
            rows = well_formed_rows(result["output"]) or []
            reference_cases.append(
                {
                    "case": case.case_id,
                    "rows": len(rows),
                    "distinct_ids": result["identity"]["distinct_ids"],
                    "quantities": len({row["quantity"] for row in rows}),
                    "engine_rows": len(case.expected_rows),
                    "engine_distinct_ids": len({r["measurement_id"] for r in case.expected_rows}),
                    "differences": ordered_differences(case.expected_rows, rows)
                    if result["status"] == "ok"
                    else None,
                    "own_aggregation_rows": dict(
                        Counter(
                            r["quantity"]
                            for r in case.expected_rows
                            if r["quantity"] in OWN_AGGREGATION
                        )
                    ),
                    "stats_equal": result["engine"]["stats_equal"],
                }
            )
        programs = []
        for entry in inventory["programs"]:
            source = None
            if entry["script"] is not None:
                source = (HERE / "results" / entry["run"] / entry["script"]).read_text(
                    encoding="utf-8"
                )
                if digest(source) != entry["sha256"]:
                    raise SystemExit(f"{entry['script']} differs from programs.json; stopping")
            key = f"{entry['run']}/{entry['mode']}/{entry['seen_vendor']}/s{entry['sample']}"
            results = replay_program(
                source, entry["mode"], cases, reference_outputs, args.exec_timeout
            )
            outputs[key] = {r["case"]: r.pop("output") for r in results}
            summary = summarise_program(results)
            programs.append({**entry, "key": key, "results": results, "summary": summary})
            print(
                f"[{key}] {summary['statuses']} contract-exact "
                f"{summary['contract_exact']} level {summary['level']}",
                flush=True,
            )
        outputs["reference"] = {r["case"]: r.pop("output") for r in reference_results}

    if record.exists():  # a second run keeps every file of the first (PROTOCOL.md, Procedure)
        earlier = len(list(results_dir.glob("replay.run*.json"))) + 1
        for name in ("replay.json", "outputs.json.gz", "report.md"):
            path = results_dir / name
            if path.exists():
                stem, _, suffix = name.partition(".")
                path.rename(results_dir / f"{stem}.run{earlier}.{suffix}")
    result = {
        "meta": _provenance(args.metadata_root)
        | {
            "frozen": {"engine_commit": ENGINE_COMMIT, "metadata_commit": METADATA_COMMIT},
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "timeout_s": args.exec_timeout,
            "rerun_reason": args.rerun_reason,
        },
        "gates": gates,
        "controls": controls,
        "cases": json.loads((REPLAY / "cases" / "manifest.json").read_text(encoding="utf-8"))[
            "cases"
        ],
        "reference": {
            "results": reference_results,
            "summary": summarise_program(reference_results),
            "cases": reference_cases,
        },
        "programs": programs,
    }
    record.write_text(json.dumps(result, indent=1), encoding="utf-8")
    with gzip.open(results_dir / "outputs.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(outputs, handle)
    report = build_replay_report(result)
    (results_dir / "report.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--metadata-root", type=Path, default=REPO.parent / "secha-metadata")
    parser.add_argument(
        "--landing-root", type=Path, default=REPO.parent / "secha-ingestion" / "data" / "landing"
    )
    parser.add_argument("--cases", type=Path, default=HERE / "cases")
    parser.add_argument("--exec-timeout", type=float, default=60.0, help="seconds per program run")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("build-cases")
    sub.add_parser("selfcheck")

    dry = sub.add_parser("dry-run")
    dry.add_argument("--mode", choices=MODES, default="snapshot")
    dry.add_argument("--vendor", choices=VENDORS, default="mx_electrix")

    probe = sub.add_parser("probe-context")
    probe.add_argument("--model", required=True)
    probe.add_argument("--endpoint")
    probe.add_argument("--tokens", type=int, default=12000)
    probe.add_argument("--repeats", type=int, default=3)
    probe.add_argument("--timeout", type=float, default=600.0)

    run = sub.add_parser("run")
    run.add_argument("--models", nargs="+", default=["phi4-14b", "codestral-2508"])
    run.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    run.add_argument("--vendors", nargs="+", choices=VENDORS, default=list(VENDORS))
    run.add_argument("--endpoint", help="override the per-model default endpoint")
    run.add_argument("--n", type=int, default=5, help="samples per cell, for pass@k")
    run.add_argument("--temperature", type=float, default=0.2)
    run.add_argument("--max-tokens", type=int, default=6000)
    run.add_argument("--repair", type=int, default=1, help="repair attempts using example feedback")
    run.add_argument("--timeout", type=float, default=900.0, help="seconds per model request")
    run.add_argument("--delay", type=float, default=0.0, help="pause between model requests")
    run.add_argument("--out", type=Path, default=HERE / "results" / "run")

    report = sub.add_parser("report")
    report.add_argument("--results", type=Path, nargs="+", required=True)
    report.add_argument("--out", type=Path, help="where to write the merged report")

    inventory = sub.add_parser("inventory")
    inventory.add_argument("--write", action="store_true", help="record, instead of check")

    sub.add_parser("replay-cases")
    replay = sub.add_parser("replay")
    replay.add_argument("--gates-only", action="store_true", help="check G1 to G7 and stop")
    replay.add_argument(
        "--rerun-reason", help="allow a second run, for a harness fault, and record why"
    )

    args = parser.parse_args()
    commands = {
        "build-cases": cmd_build_cases,
        "selfcheck": cmd_selfcheck,
        "dry-run": cmd_dry_run,
        "probe-context": cmd_probe_context,
        "run": cmd_run,
        "report": cmd_report,
        "inventory": cmd_inventory,
        "replay-cases": cmd_replay_cases,
        "replay": cmd_replay,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
