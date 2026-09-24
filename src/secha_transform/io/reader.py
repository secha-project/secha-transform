"""Read raw records and device factors from the landing zone (fsspec).

Reads only, never transforms. The vendor's source schema drives everything: the access
descriptor (`access.layout`) resolves partitions, the format descriptor (`format:`) selects
the parser (JSON arrays/objects, header-less DSV whose values stay strings, or Parquet,
whose values arrive typed; value interpretation is the engine's job). A declared format the
reader cannot honour raises. Records stream lazily so multi-million-row inputs never sit in
memory at once.

A layout names its partitions with placeholders (`date={date}/meter={meter}`,
`export={export}/part={part}`). A caller selects partitions by giving some placeholder
values; the rest match any value. For a source with no row key
(`record.row_id_from: payload_position`) each record is stamped with its partition and its
index in the payload, which the engine uses as the row id: the payload is immutable once
landed, so the position is stable.

Mirrors the secha-ingestion landing layout: partitions hold `<sha16>.<ext>` payloads plus
`.meta.json` envelope sidecars; multiple payloads in one partition are snapshots, of which
only the latest (by the envelope's `fetched_at`) is read.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import fsspec
import pyarrow.parquet as pq

from secha_transform.engine.models import PAYLOAD_POSITION_FIELD
from secha_transform.metadata.loader import MetadataBundle

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_PARQUET_BATCH_ROWS = 65_536  # rows per Arrow batch: bounded memory, few Python round trips


def layout_keys(layout: str) -> list[str]:
    """The placeholders of an access layout, in order ('date={date}/meter={meter}' -> both)."""
    return _PLACEHOLDER.findall(layout)


def _layout_regex(layout: str) -> re.Pattern[str]:
    """A pattern matching a partition path of the layout, capturing each placeholder."""
    pieces = _PLACEHOLDER.split(layout)  # literal, key, literal, key, ..., literal
    parts = [
        re.escape(piece) if index % 2 == 0 else f"(?P<{piece}>[^/]+)"
        for index, piece in enumerate(pieces)
    ]
    return re.compile("(?:^|/)" + "".join(parts) + "$")


@dataclass(frozen=True)
class Partition:
    """One landing partition: its placeholder values and its directory."""

    values: dict[str, str]
    directory: str

    @property
    def identity(self) -> str:
        """The partition as `key=value` pairs in layout order, e.g. `export=…/part=…`."""
        return "/".join(f"{key}={value}" for key, value in self.values.items())


def _latest_payload(fs: Any, directory: str) -> str | None:
    """Pick the newest landed snapshot in a partition.

    Ingestion keeps every changed snapshot side by side (immutability); reading them all
    would duplicate (or worse, contradict) records. Recency comes from the envelope
    sidecar's `fetched_at` (path as a deterministic tie-breaker), which implements the
    source schema's `snapshot_selection: latest_by_fetched_at`.
    """
    if not fs.exists(directory):
        return None
    paths: list[str] = [
        str(path)
        for path in fs.glob(f"{directory}/*")
        if not str(path).endswith((".meta.json", ".tmp"))
    ]
    if not paths:
        return None

    def recency(path: str) -> tuple[str, str]:
        meta_path = path.rsplit(".", 1)[0] + ".meta.json"
        fetched_at = ""
        if fs.exists(meta_path):
            with fs.open(meta_path, "rb") as handle:
                fetched_at = str(json.loads(handle.read()).get("fetched_at") or "")
        return (fetched_at, path)

    return max(paths, key=recency)


def parse_dsv_records(body: bytes, source_schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Parse header-less delimiter-separated bytes into records named by the declared fields.

    Values stay strings: the reader parses STRUCTURE; value interpretation (numbers,
    epochs) belongs to the engine, driven by the mapping. Structurally malformed lines
    are skipped here; ingestion already counted them in the landing envelope.
    """
    fmt = source_schema.get("format", {})
    if fmt.get("header", False):
        raise ValueError("DSV header rows are not implemented; declare fields + header: false")
    encoding = fmt.get("encoding", "utf-8")
    delimiter = fmt.get("delimiter", ",")
    names = [field["name"] for field in source_schema.get("fields", [])]
    for raw_line in io.BytesIO(body):
        line = raw_line.decode(encoding).rstrip("\r\n")
        if not line:
            continue
        parts = line.split(delimiter)
        if len(parts) != len(names):
            continue
        yield dict(zip(names, parts, strict=True))


def _read_payload_records(
    fs: Any, directory: str, source_schema: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    path = _latest_payload(fs, directory)
    if path is None:
        return
    fmt = source_schema.get("format", {})
    fmt_type = fmt.get("type", "json")
    if fmt_type == "parquet":
        # streamed batch by batch; values arrive typed, as the export declared them
        with fs.open(path, "rb") as handle:
            for batch in pq.ParquetFile(handle).iter_batches(batch_size=_PARQUET_BATCH_ROWS):
                yield from batch.to_pylist()
        return
    with fs.open(path, "rb") as handle:
        # bytes + explicit UTF-8 default: text mode would use the platform encoding
        # (cp1252 on Windows) and misdecode e.g. Finnish characters in the raw payload
        body = handle.read()
    if fmt_type == "json":
        data = json.loads(body.decode(fmt.get("encoding", "utf-8")))
        if isinstance(data, list):
            yield from data
        elif isinstance(data, dict):
            yield data
        return
    if fmt_type == "csv":
        yield from parse_dsv_records(body, source_schema)
        return
    raise ValueError(f"format type '{fmt_type}' is not implemented by this engine")


def iter_partitions(
    landing_root: str, source_schema: dict[str, Any], **selection: str | None
) -> Iterator[Partition]:
    """The landing partitions the selection names, in sorted order.

    `selection` gives values for some of the layout's placeholders; a placeholder left out
    (or given as None) matches any value. A key the layout does not have raises.
    """
    fs, base = fsspec.core.url_to_fs(landing_root)
    root = str(base).rstrip("/")
    layout: str = source_schema["access"]["layout"]
    keys = layout_keys(layout)
    unknown = sorted(set(selection) - set(keys))
    if unknown:
        raise ValueError(f"layout {layout!r} has no placeholder(s) {unknown}; it has {keys}")
    chosen = {key: value for key, value in selection.items() if value is not None}
    pattern = layout.format(**{key: chosen.get(key, "*") for key in keys})
    if len(chosen) == len(keys):
        candidate = f"{root}/{pattern}"  # fully specified: no listing needed
        directories = [candidate] if fs.isdir(candidate) else []
    else:
        directories = sorted(
            str(path) for path in fs.glob(f"{root}/{pattern}") if fs.isdir(str(path))
        )
    matcher = _layout_regex(layout)
    for directory in directories:
        found = matcher.search(directory.replace("\\", "/"))
        values = {key: found.group(key) if found else chosen[key] for key in keys}
        yield Partition(values=values, directory=directory)


def read_partition(
    landing_root: str, partition: Partition, source_schema: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """Yield one partition's raw records; stamp each with its position when the source
    declares `record.row_id_from: payload_position`."""
    fs, _ = fsspec.core.url_to_fs(landing_root)
    records = _read_payload_records(fs, partition.directory, source_schema)
    if (source_schema.get("record") or {}).get("row_id_from") != "payload_position":
        yield from records
        return
    for index, record in enumerate(records):
        record[PAYLOAD_POSITION_FIELD] = f"{partition.identity}:{index}"
        yield record


def read_records(
    landing_root: str,
    source_schema: dict[str, Any],
    date: str | None = None,
    meter: str | None = None,
    **selection: str | None,
) -> Iterator[dict[str, Any]]:
    """Yield the raw records of every selected partition, per the source schema.

    `date` and `meter` are the placeholders the first two vendors' layouts use; any other
    placeholder is selected by keyword. Unselected placeholders match every partition.
    """
    if date is not None:
        selection["date"] = date
    if meter is not None:
        selection["meter"] = meter
    for partition in iter_partitions(landing_root, source_schema, **selection):
        yield from read_partition(landing_root, partition, source_schema)


def read_device_factors(
    landing_root: str, vendor: str, date: str, bundle: MetadataBundle
) -> dict[str, dict[str, float]]:
    """Build {meter_id: {"uk": ..., "ik": ...}} from the raw `/meters/` records.

    The device-record field names are taken from the vendor's `source_schema.device_factors`,
    keeping this vendor-blind. Only meaningful for sources that declare device factors.
    """
    fs, base = fsspec.core.url_to_fs(landing_root)
    meters_dir = f"{str(base).rstrip('/')}/vendor={vendor}/source=meters/date={date}"
    path = _latest_payload(fs, meters_dir)
    devices: list[dict[str, Any]] = []
    if path is not None:
        with fs.open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
        devices = data if isinstance(data, list) else [data]
    factor_cfg = bundle.source_schema.get("device_factors", {})
    uk_field = factor_cfg.get("voltage_factor", "uk")
    ik_field = factor_cfg.get("current_factor", "ik")
    factors: dict[str, dict[str, float]] = {}
    for device in devices:
        factors[str(device.get("id"))] = {
            "uk": float(device[uk_field]),
            "ik": float(device[ik_field]),
        }
    return factors
