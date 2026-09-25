"""The Kempower replay's program inventory: provenance, digests and roles, no execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from codegen.replay import RUNS, build_inventory, digest, inventory_differences, role

PROGRAM = "import json, sys\njson.dump({'rows': [], 'stats': {}}, sys.stdout)\n"


def _record_runs(results: Path, cache: Path, program: str, on_disk: str) -> None:
    """Three recorded runs of one sample each, and a cached reply holding `program`."""
    cache.mkdir(parents=True)
    reply = {"text": f"Here it is:\n```python\n{program}```\n"}
    (cache / "reply.json").write_text(json.dumps(reply), encoding="utf-8")
    for run in RUNS:
        script = "scripts\\interpreter\\mx_electrix\\model\\s0_a1.py"  # as Windows recorded it
        path = results / run / "scripts" / "interpreter" / "mx_electrix" / "model" / "s0_a1.py"
        path.parent.mkdir(parents=True)
        path.write_bytes(on_disk.encode("utf-8"))
        raw = {
            "meta": {
                "engine_commit": "a0510a9",
                "metadata_commit": "95de96b",
                "contract_sha256": "325cc47c89a5",
                "created_at": "2026-09-15T00:00:00+00:00",
            },
            "samples": [
                {
                    "model": f"vendor/{run}",
                    "mode": "interpreter",
                    "seen_vendor": "mx_electrix",
                    "sample": 0,
                    "attempts": [{"attempt": 0, "script": None}, {"attempt": 1, "script": script}],
                }
            ],
        }
        (results / run / "raw.json").write_text(json.dumps(raw), encoding="utf-8")


def test_the_final_program_is_inventoried_whatever_its_line_endings(tmp_path: Path) -> None:
    _record_runs(tmp_path / "results", tmp_path / "cache", PROGRAM, PROGRAM.replace("\n", "\r\n"))

    inventory = build_inventory(tmp_path / "results", tmp_path / "cache")

    assert [p["run"] for p in inventory["programs"]] == sorted(RUNS)
    kimi = next(p for p in inventory["programs"] if p["run"] == "kimi-k3")
    assert kimi["attempt"] == 1  # the program after repair, not the first attempt
    assert kimi["script"] == "scripts/interpreter/mx_electrix/model/s0_a1.py"
    assert kimi["sha256"] == digest(PROGRAM)
    assert kimi["role"] == "primary"
    assert inventory["runs"]["kimi-k3"]["engine_commit"] == "a0510a9"


def test_a_program_edited_after_its_run_is_refused(tmp_path: Path) -> None:
    _record_runs(tmp_path / "results", tmp_path / "cache", PROGRAM, PROGRAM + "# edited\n")

    with pytest.raises(ValueError, match="not the program of any cached reply"):
        build_inventory(tmp_path / "results", tmp_path / "cache")


def test_a_changed_program_is_named(tmp_path: Path) -> None:
    _record_runs(tmp_path / "results", tmp_path / "cache", PROGRAM, PROGRAM)
    recorded = build_inventory(tmp_path / "results", tmp_path / "cache")
    current = json.loads(json.dumps(recorded))
    current["programs"][0]["sha256"] = "0" * 64

    assert inventory_differences(recorded, recorded) == []
    (difference,) = inventory_differences(recorded, current)
    assert difference.startswith("program codestral-2508/interpreter/mx_electrix/0")


def test_roles_follow_the_protocol() -> None:
    assert role("kimi-k3", "interpreter") == role("codestral-2508", "interpreter") == "primary"
    assert role("phi4-14b", "interpreter") == "secondary"
    assert {role(run, "snapshot") for run in RUNS} == {"floor"}
