"""The Kempower replay: programs recorded in September, run on a vendor none of them saw.

This module freezes which programs the replay runs (kempower_replay/PROTOCOL.md). Nothing
here executes a program. For every sample of the three recorded runs, the inventory names
the program its final attempt wrote and its SHA-256, and it refuses a program that is not
exactly what `extract_code` takes from one of the cached model replies, so a program edited
after its run can never be replayed as the model's.

A program's text is the one the harness executed. The file on disk was written in text mode
and may carry the platform's line endings, so it is read back with universal newlines and
digested as UTF-8 with LF newlines.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PureWindowsPath
from typing import Any

from codegen.client import extract_code

RUNS = ("kimi-k3", "codestral-2508", "phi4-14b")
PRIMARY_RUNS = frozenset({"kimi-k3", "codestral-2508"})


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def role(run: str, mode: str) -> str:
    """Primary: the two arms the plan named; secondary: phi4-14b; floor: snapshot programs,
    whose configuration is another vendor's written into them."""
    if mode == "snapshot":
        return "floor"
    return "primary" if run in PRIMARY_RUNS else "secondary"


def replied_programs(cache: Path) -> set[str]:
    """Digests of every program that `extract_code` finds in a cached model reply."""
    found: set[str] = set()
    for path in sorted(cache.rglob("*")):
        if not path.is_file():
            continue
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        code = extract_code(blob.get("text")) if isinstance(blob, dict) else None
        if code is not None:
            found.add(digest(code))
    return found


def build_inventory(results: Path, cache: Path) -> dict[str, Any]:
    """Every sample's final program, its role and digest, and each run's provenance.

    The final attempt is the program after repair, which is how the recorded cross-vendor
    and drift results describe a sample (findings.md).
    """
    from_replies = replied_programs(cache)
    runs: dict[str, Any] = {}
    programs: list[dict[str, Any]] = []
    for run in RUNS:
        raw_path = results / run / "raw.json"
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        meta = raw["meta"]
        runs[run] = {
            "raw_json_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "engine_commit": meta["engine_commit"],
            "metadata_commit": meta["metadata_commit"],
            "contract_sha256": meta["contract_sha256"],
            "created_at": meta["created_at"],
        }
        for sample in raw["samples"]:
            final = sample["attempts"][-1]
            entry: dict[str, Any] = {
                "run": run,
                "model": sample["model"],
                "mode": sample["mode"],
                "seen_vendor": sample["seen_vendor"],
                "sample": int(sample["sample"]),
                "attempt": int(final["attempt"]),
                "role": role(run, sample["mode"]),
                "script": None,
                "sha256": None,
            }
            if final["script"] is not None:
                script = PureWindowsPath(final["script"]).as_posix()
                text = (results / run / script).read_text(encoding="utf-8")
                entry["script"], entry["sha256"] = script, digest(text)
                if entry["sha256"] not in from_replies:
                    raise ValueError(f"{run}/{script} is not the program of any cached reply")
            programs.append(entry)
    programs.sort(key=lambda p: (p["run"], p["mode"], p["seen_vendor"], p["sample"]))
    return {"runs": runs, "programs": programs}


def inventory_differences(recorded: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """What changed between a recorded inventory and one built now; empty when identical."""
    if recorded == current:
        return []
    differences = [
        f"run {run}: recorded {recorded['runs'].get(run)}, now {current['runs'].get(run)}"
        for run in sorted(set(recorded["runs"]) | set(current["runs"]))
        if recorded["runs"].get(run) != current["runs"].get(run)
    ]
    before = {(p["run"], p["mode"], p["seen_vendor"], p["sample"]): p for p in recorded["programs"]}
    after = {(p["run"], p["mode"], p["seen_vendor"], p["sample"]): p for p in current["programs"]}
    differences += [
        f"program {'/'.join(map(str, key))}: recorded {before.get(key)}, now {after.get(key)}"
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]
    return differences or ["the inventories differ in form"]
