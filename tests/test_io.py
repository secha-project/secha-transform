"""IO tests: snapshot selection, formats + UTF-8 in the reader; idempotency in the writer."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.dataset as ds

from secha_transform.engine.models import CanonicalRow
from secha_transform.io.reader import read_records
from secha_transform.io.writer import write_canonical_parquet

WIDE_SCHEMA = {
    "access": {"layout": "vendor=demo/source=measurements/date={date}/meter={meter}"},
    "format": {"type": "json", "encoding": "utf-8"},
    "fields": [],
}
LONG_SCHEMA = {
    "access": {"layout": "vendor=demo2/source=daily_dump/date={date}"},
    "format": {"type": "csv", "delimiter": "\t", "header": False, "encoding": "utf-8"},
    "fields": [
        {"name": "measurement_id", "type": "long"},
        {"name": "value", "type": "string"},
        {"name": "timestamp", "type": "long"},
    ],
}


def _land(directory: Path, name: str, body: bytes, fetched_at: str, ext: str = "json") -> None:
    """Mimic the secha-ingestion landing layout: payload + envelope sidecar."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.{ext}").write_bytes(body)
    meta = json.dumps({"fetched_at": fetched_at}).encode("utf-8")
    (directory / f"{name}.meta.json").write_bytes(meta)


def _json_body(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_reader_picks_latest_snapshot(tmp_path: Path) -> None:
    """Ingestion keeps every changed snapshot; the reader must use only the newest one."""
    part = tmp_path / "vendor=demo" / "source=measurements" / "date=2025-01-01" / "meter=1"
    _land(part, "aaaa", _json_body([{"id": 1, "v": 1.0}]), "2026-01-01T00:00:00+00:00")
    _land(part, "bbbb", _json_body([{"id": 1, "v": 2.0}]), "2026-01-02T00:00:00+00:00")

    records = list(read_records(str(tmp_path), WIDE_SCHEMA, "2025-01-01", "1"))

    assert records == [{"id": 1, "v": 2.0}]  # latest only: no duplicates, no stale values


def test_reader_decodes_utf8(tmp_path: Path) -> None:
    """Raw payloads must be decoded as UTF-8, not the platform encoding."""
    part = tmp_path / "vendor=demo" / "source=measurements" / "date=2025-01-01" / "meter=1"
    _land(part, "aaaa", _json_body([{"id": 1, "location": "Sähkötalo"}]), "2026-01-01T00:00:00Z")

    records = list(read_records(str(tmp_path), WIDE_SCHEMA, "2025-01-01", "1"))

    assert records[0]["location"] == "Sähkötalo"


def test_reader_parses_dsv_per_format_descriptor(tmp_path: Path) -> None:
    """A long/DSV source is read via its layout (no meter) and parsed per `format:`."""
    part = tmp_path / "vendor=demo2" / "source=daily_dump" / "date=2026-06-15"
    body = b"23501\t49.987724\t1781470800246\nnot a triple\n23502\t-45.1\t1781470800246\n"
    _land(part, "cccc", body, "2026-06-16T00:00:00+00:00", ext="csv")

    records = list(read_records(str(tmp_path), LONG_SCHEMA, "2026-06-15"))

    assert records == [  # strings preserved; malformed line skipped (counted at landing)
        {"measurement_id": "23501", "value": "49.987724", "timestamp": "1781470800246"},
        {"measurement_id": "23502", "value": "-45.1", "timestamp": "1781470800246"},
    ]


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
