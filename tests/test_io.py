"""IO tests: snapshot selection, formats, layouts and positions in the reader; typing,
null partitions and idempotency in the writer."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest

from secha_transform.engine.models import PAYLOAD_POSITION_FIELD, CanonicalRow
from secha_transform.io.reader import iter_partitions, read_records
from secha_transform.io.writer import (
    ENTITY_PARTITIONING,
    MEASUREMENT_PARTITIONING,
    write_canonical_parquet,
    write_entity_parquet,
)

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


# --- Parquet payloads, any layout placeholders, positions --------------------------------

EXPORT_SCHEMA = {
    "access": {"layout": "vendor=demo3/source=export/export={export}/part={part}"},
    "format": {"type": "parquet", "encoding": "utf-8"},
    "record": {"row_id_from": "payload_position"},
    "fields": [],
}


def _land_parquet(root: Path, export: str, part: str, rows: list[dict]) -> None:
    directory = root / "vendor=demo3" / "source=export" / f"export={export}" / f"part={part}"
    directory.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows), directory / "dddd.parquet")
    (directory / "dddd.meta.json").write_bytes(b'{"fetched_at": "2026-09-24T00:00:00Z"}')


def test_reader_streams_parquet_typed_and_stamps_positions(tmp_path: Path) -> None:
    _land_parquet(
        tmp_path, "e1", "00000-c000", [{"tx": "a", "soc": 20.0}, {"tx": "a", "soc": 21.5}]
    )

    records = list(read_records(str(tmp_path), EXPORT_SCHEMA))

    assert records == [
        {"tx": "a", "soc": 20.0, PAYLOAD_POSITION_FIELD: "export=e1/part=00000-c000:0"},
        {"tx": "a", "soc": 21.5, PAYLOAD_POSITION_FIELD: "export=e1/part=00000-c000:1"},
    ]


def test_reader_selects_by_any_placeholder_and_wildcards_the_rest(tmp_path: Path) -> None:
    _land_parquet(tmp_path, "e1", "00001-c000", [{"n": 2}])
    _land_parquet(tmp_path, "e1", "00000-c000", [{"n": 1}])
    _land_parquet(tmp_path, "e2", "00000-c000", [{"n": 3}])

    everything = [p.identity for p in iter_partitions(str(tmp_path), EXPORT_SCHEMA)]
    one_export = [p.identity for p in iter_partitions(str(tmp_path), EXPORT_SCHEMA, export="e1")]
    exact = list(read_records(str(tmp_path), EXPORT_SCHEMA, export="e1", part="00001-c000"))

    assert everything == [
        "export=e1/part=00000-c000",
        "export=e1/part=00001-c000",
        "export=e2/part=00000-c000",
    ]
    assert one_export == everything[:2]
    assert [r["n"] for r in exact] == [2]


def test_reader_refuses_a_placeholder_the_layout_lacks(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no placeholder"):
        list(iter_partitions(str(tmp_path), EXPORT_SCHEMA, date="2025-01-01"))


def test_reader_yields_nothing_for_a_partition_never_landed(tmp_path: Path) -> None:
    assert list(read_records(str(tmp_path), EXPORT_SCHEMA, export="e9", part="00000-c000")) == []


def test_reader_leaves_records_unstamped_when_the_source_has_a_row_key(tmp_path: Path) -> None:
    part = tmp_path / "vendor=demo" / "source=measurements" / "date=2025-01-01" / "meter=1"
    _land(part, "aaaa", _json_body([{"id": 1, "v": 1.0}]), "2026-01-01T00:00:00Z")
    assert list(read_records(str(tmp_path), WIDE_SCHEMA, "2025-01-01", "1")) == [
        {"id": 1, "v": 1.0}
    ]


# --- rows without clock time; typed files; entity datasets ---------------------------------


def _row_without_clock(i: int) -> CanonicalRow:
    row = _row(i)
    return CanonicalRow(
        **{**row.to_dict(), "ts_utc": None, "session_id": "s", "ts_session_offset_s": 10}
    )


def test_writer_puts_rows_without_clock_time_in_the_null_partition(tmp_path: Path) -> None:
    root = tmp_path / "canonical"
    write_canonical_parquet([_row_without_clock(1)], str(root), run_tag="e1-p0")

    (directory,) = [p for p in (root / "source_vendor=demo").iterdir()]
    table = ds.dataset(str(root), partitioning=MEASUREMENT_PARTITIONING).to_table()

    assert directory.name == "event_date=__HIVE_DEFAULT_PARTITION__"
    assert table.column("event_date").to_pylist() == [None]  # a real null, not a string


def test_a_dataset_of_only_undated_rows_needs_the_declared_partitioning(tmp_path: Path) -> None:
    """Inference has no non-null event_date to type; the declared partitioning does not need one."""
    root = tmp_path / "canonical"
    write_canonical_parquet([_row_without_clock(1)], str(root), run_tag="e1-p0")
    with pytest.raises(pa.ArrowInvalid, match="non-null"):
        ds.dataset(str(root), partitioning="hive").to_table()
    assert ds.dataset(str(root), partitioning=MEASUREMENT_PARTITIONING).to_table().num_rows == 1


def test_writer_types_every_file_alike_so_mixed_sources_read_together(tmp_path: Path) -> None:
    """A batch without clock time must not type ts_utc as null while another types it string."""
    root = tmp_path / "canonical"
    write_canonical_parquet([_row_without_clock(1)], str(root), run_tag="kempower-like")
    write_canonical_parquet([_row(2)], str(root), run_tag="clocked")

    table = ds.dataset(str(root), partitioning=MEASUREMENT_PARTITIONING).to_table()

    assert table.schema.field("ts_utc").type == pa.string()
    assert table.schema.field("ts_session_offset_s").type == pa.int64()
    assert sorted(table.column("ts_utc").to_pylist(), key=str) == ["2025-01-01T00:00:00Z", None]


def test_entity_writer_types_columns_by_the_canonical_schema(tmp_path: Path) -> None:
    root = tmp_path / "charging_session"
    field_types = [
        ("session_id", "string"),
        ("source_vendor", "enum:source_vendor"),
        ("ev_brand", "string"),
        ("cal_year", "int"),
    ]
    rows = [{"session_id": "s1", "source_vendor": "demo", "ev_brand": None, "cal_year": 2025}]
    write_entity_parquet(rows, str(root), field_types, run_tag="e1-p0")
    write_entity_parquet(rows, str(root), field_types, run_tag="e1-p0")  # re-run same scope

    table = ds.dataset(str(root), partitioning=ENTITY_PARTITIONING).to_table()

    assert table.num_rows == 1
    assert table.schema.field("ev_brand").type == pa.string()  # all null, still typed
    assert table.schema.field("cal_year").type == pa.int64()
