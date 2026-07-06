"""Golden test: the engine must satisfy the contract defined in secha-metadata.

The metadata repo's `expected_canonical.json` lists known-correct canonical rows for the sample raw
record. The engine must produce each of them (matched on the engine-independent fields).
"""

from __future__ import annotations

from pathlib import Path

from conftest import load_fixture
from secha_transform.engine.transform import transform_records
from secha_transform.metadata.loader import load_bundle

FACTORS = {"22": {"uk": 1.0, "ik": 1.0}}  # device 22: factors are 1, so scaled value == raw


def _matches(produced: dict, expected: dict) -> bool:
    return all(produced.get(key) == value for key, value in expected.items())


def test_golden_rows_are_produced(metadata_root: Path) -> None:
    bundle = load_bundle(metadata_root, "mx_electrix")
    raw = load_fixture(metadata_root, "raw_measurements_sample.json")
    expected = load_fixture(metadata_root, "expected_canonical.json")

    produced = [row.to_dict() for row in transform_records(raw, bundle, FACTORS).rows]

    for exp in expected:
        assert any(_matches(p, exp) for p in produced), f"missing expected canonical row: {exp}"


def test_no_identity_collapse(metadata_root: Path) -> None:
    bundle = load_bundle(metadata_root, "mx_electrix")
    raw = load_fixture(metadata_root, "raw_measurements_sample.json")
    rows = transform_records(raw, bundle, FACTORS).rows
    ids = [row.measurement_id for row in rows]
    assert len(ids) == len(set(ids)), "duplicate measurement_id -> identity collapse"


def test_deterministic(metadata_root: Path) -> None:
    bundle = load_bundle(metadata_root, "mx_electrix")
    raw = load_fixture(metadata_root, "raw_measurements_sample.json")

    def run() -> list[dict]:
        return [
            {k: v for k, v in row.to_dict().items() if k != "ingested_at"}
            for row in transform_records(raw, bundle, FACTORS).rows
        ]

    assert run() == run()
