"""IO tests: snapshot selection + UTF-8 in the reader; run-scoped idempotency in the writer."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.dataset as ds

from secha_transform.engine.models import CanonicalRow
from secha_transform.io.reader import read_measurements
from secha_transform.io.writer import write_canonical_parquet


def _land(directory: Path, name: str, payload: object, fetched_at: str) -> None:
    """Mimic the secha-ingestion landing layout: payload + envelope sidecar."""
    directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    (directory / f"{name}.json").write_bytes(body)
    meta = json.dumps({"fetched_at": fetched_at}).encode("utf-8")
    (directory / f"{name}.meta.json").write_bytes(meta)


def test_reader_picks_latest_snapshot(tmp_path: Path) -> None:
    """Ingestion keeps every changed snapshot; the reader must use only the newest one."""
    part = tmp_path / "vendor=demo" / "source=measurements" / "date=2025-01-01" / "meter=1"
    _land(part, "aaaa", [{"id": 1, "v": 1.0}], "2026-01-01T00:00:00+00:00")
    _land(part, "bbbb", [{"id": 1, "v": 2.0}], "2026-01-02T00:00:00+00:00")  # revised, newer

    records = read_measurements(str(tmp_path), "demo", "2025-01-01", "1")

    assert records == [{"id": 1, "v": 2.0}]  # latest only — no duplicates, no stale values


def test_reader_decodes_utf8(tmp_path: Path) -> None:
    """Raw payloads must be decoded as UTF-8, not the platform encoding."""
    part = tmp_path / "vendor=demo" / "source=measurements" / "date=2025-01-01" / "meter=1"
    _land(part, "aaaa", [{"id": 1, "location": "Sähkötalo"}], "2026-01-01T00:00:00+00:00")

    records = read_measurements(str(tmp_path), "demo", "2025-01-01", "1")

    assert records[0]["location"] == "Sähkötalo"


def _row(i: int) -> CanonicalRow:
    return CanonicalRow(
        measurement_id=f"id-{i}",
        source_vendor="demo",
        source_dataset="measurements",
        device_id="demo:meter:1",
        ts_utc="2025-01-01T00:00:00Z",
        quantity="voltage",
        phase="L1",
        variant="none",
        harmonic_order=None,
        value=float(i),
        unit="V",
        aggregation="average",
        interval_s=60,
        quality="ok",
        source_row_id=str(i),
        schema_version="1.0.0",
        ingested_at="2026-01-01T00:00:00+00:00",
    )


def _count(root: Path) -> int:
    return ds.dataset(str(root), partitioning="hive").to_table().num_rows


def test_writer_rerun_same_scope_is_idempotent(tmp_path: Path) -> None:
    rows = [_row(1), _row(2)]
    root = tmp_path / "canonical"
    write_canonical_parquet(rows, str(root), run_tag="2025-01-01-meter-1")
    write_canonical_parquet(rows, str(root), run_tag="2025-01-01-meter-1")  # re-run same scope
    assert _count(root) == 2  # replaced, not appended


def test_writer_different_scopes_coexist(tmp_path: Path) -> None:
    """Two scopes sharing a date partition (e.g. two meters) must not clobber each other."""
    root = tmp_path / "canonical"
    write_canonical_parquet([_row(1)], str(root), run_tag="2025-01-01-meter-1")
    write_canonical_parquet([_row(2)], str(root), run_tag="2025-01-01-meter-2")
    assert _count(root) == 2
