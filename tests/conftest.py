from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def metadata_root() -> Path:
    """Resolve the secha-metadata checkout (the rulebook the engine is tested against).

    Uses SECHA_METADATA_ROOT if set, else the sibling repo. Skips if not found, so the
    self-contained unit tests still run in a bare environment.
    """
    env = os.environ.get("SECHA_METADATA_ROOT")
    root = Path(env) if env else (Path(__file__).resolve().parents[1].parent / "secha-metadata")
    root = root.resolve()
    if not (root / "canonical" / "canonical_schema.yaml").exists():
        pytest.skip(f"secha-metadata not found at {root}; set SECHA_METADATA_ROOT")
    return root


def load_fixture(metadata_root: Path, name: str) -> list[dict]:
    path = metadata_root / "tests" / "fixtures" / "mx_electrix" / name
    return json.loads(path.read_text(encoding="utf-8"))
