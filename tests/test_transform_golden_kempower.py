"""Golden test, vendor #3: the engine must satisfy the Kempower contract in secha-metadata.

The fixture is synthetic, because the export is partner data, but shaped like it. It runs
the whole read path: landed as one Parquet payload in the layout's export/part partition,
read back by the reader (which stamps each record's position), then transformed. The
expected rows keep two readings that share a session and an offset, skip a missing
temperature, flag a negative voltage and a state of charge above 100, reject a record with
no session, and produce one charging_session row per session.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import land_kempower_sample
from secha_transform.engine.models import TransformResult
from secha_transform.engine.transform import transform_records
from secha_transform.io.reader import read_records
from secha_transform.metadata.loader import load_bundle

VENDOR = "kempower"


def _fixture(metadata_root: Path, name: str) -> Any:
    path = metadata_root / "tests" / "fixtures" / VENDOR / name
    return json.loads(path.read_text(encoding="utf-8"))


def _run(metadata_root: Path, tmp_path: Path) -> TransformResult:
    """Land the raw sample as the ingestion layer would, then read and transform it."""
    bundle = load_bundle(metadata_root, VENDOR)
    land_kempower_sample(metadata_root, tmp_path / "landing")
    return transform_records(read_records(str(tmp_path / "landing"), bundle.source_schema), bundle)


def test_golden_rows_are_produced(metadata_root: Path, tmp_path: Path) -> None:
    produced = {
        (r.source_row_id, r.quantity): r.to_dict() for r in _run(metadata_root, tmp_path).rows
    }
    expected = _fixture(metadata_root, "expected_canonical.json")

    assert len(produced) == len(expected)  # nothing extra, nothing missing, nothing merged
    for row in expected:
        got = produced[(row["source_row_id"], row["quantity"])]
        assert {key: got[key] for key in row} == row


def test_golden_sessions_are_produced(metadata_root: Path, tmp_path: Path) -> None:
    sessions = _run(metadata_root, tmp_path).sessions
    assert sessions == _fixture(metadata_root, "expected_sessions.json")


def test_every_outcome_is_counted(metadata_root: Path, tmp_path: Path) -> None:
    stats = _run(metadata_root, tmp_path).stats
    assert (stats.records_in, stats.records_rejected) == (6, 1)
    assert (stats.rows_emitted, stats.rows_suspect, stats.cells_null_skipped) == (24, 2, 1)


def test_no_identity_collapse(metadata_root: Path, tmp_path: Path) -> None:
    """Two readings at the same session offset must stay two rows with two identities."""
    ids = [row.measurement_id for row in _run(metadata_root, tmp_path).rows]
    assert len(ids) == len(set(ids)), "duplicate measurement_id -> identity collapse"


def test_deterministic(metadata_root: Path, tmp_path: Path) -> None:
    def run(folder: str) -> list[dict[str, Any]]:
        rows = _run(metadata_root, tmp_path / folder).rows
        return [{k: v for k, v in row.to_dict().items() if k != "ingested_at"} for row in rows]

    assert run("first") == run("second")
