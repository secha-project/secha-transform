from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

EXPERIMENT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT))


@pytest.fixture(scope="session")
def metadata_root() -> Path:
    """The secha-metadata checkout, from SECHA_METADATA_ROOT or the sibling directory."""
    env = os.environ.get("SECHA_METADATA_ROOT")
    root = (Path(env) if env else EXPERIMENT.parents[2] / "secha-metadata").resolve()
    if not (root / "canonical" / "canonical_schema.yaml").exists():
        pytest.skip(f"secha-metadata not found at {root}; set SECHA_METADATA_ROOT")
    return root


@pytest.fixture(scope="session")
def synthetic_cases(metadata_root: Path) -> list:
    """Every case that can be built without the landing zone: no partner data involved."""
    from codegen.cases import build_cases

    return build_cases(metadata_root, landing_root=None)


@pytest.fixture(scope="session")
def frozen_cases() -> list:
    """The locally built cases, which include real landing-zone data, when they exist."""
    from codegen.cases import load_cases

    directory = EXPERIMENT / "cases"
    if not any(directory.glob("*/*.json")):
        pytest.skip("no frozen cases; run `python harness.py build-cases` first")
    return load_cases(directory)
