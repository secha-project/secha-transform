"""Golden test, vendor #2: the engine must satisfy the ProCem contract in secha-metadata.

Raw fixture = REAL 2026-06-15 triples (verified on the S: drive); expected rows include the
Helsinki-local-midnight timestamp, fundamental/Fryze variants, an explicit harmonic order,
and a counter-aggregation energy row.
"""

from __future__ import annotations

import json
from pathlib import Path

from secha_transform.engine.transform import transform_records
from secha_transform.io.reader import parse_dsv_records
from secha_transform.metadata.loader import load_bundle

VENDOR = "procem_kampusareena_pq"


def _load(metadata_root: Path) -> tuple[list[dict], list[dict]]:
    bundle = load_bundle(metadata_root, VENDOR)
    fixtures = metadata_root / "tests" / "fixtures" / VENDOR
    raw = list(
        parse_dsv_records(
            (fixtures / "raw_measurements_sample.tsv").read_bytes(), bundle.source_schema
        )
    )
    expected = json.loads((fixtures / "expected_canonical.json").read_text(encoding="utf-8"))
    return raw, expected


def _matches(produced: dict, expected: dict) -> bool:
    return all(produced.get(key) == value for key, value in expected.items())


def test_golden_rows_are_produced(metadata_root: Path) -> None:
    bundle = load_bundle(metadata_root, VENDOR)
    raw, expected = _load(metadata_root)

    produced = [row.to_dict() for row in transform_records(raw, bundle).rows]

    assert len(produced) == len(expected)  # every fixture line is mapped: nothing extra/missing
    for exp in expected:
        assert any(_matches(p, exp) for p in produced), f"missing expected canonical row: {exp}"


def test_no_identity_collapse(metadata_root: Path) -> None:
    bundle = load_bundle(metadata_root, VENDOR)
    raw, _ = _load(metadata_root)
    ids = [row.measurement_id for row in transform_records(raw, bundle).rows]
    assert len(ids) == len(set(ids)), "duplicate measurement_id -> identity collapse"


def test_deterministic(metadata_root: Path) -> None:
    bundle = load_bundle(metadata_root, VENDOR)
    raw, _ = _load(metadata_root)

    def run() -> list[dict]:
        return [
            {k: v for k, v in row.to_dict().items() if k != "ingested_at"}
            for row in transform_records(raw, bundle).rows
        ]

    assert run() == run()
