"""Read raw records and device factors from the landing zone (fsspec).

Reads only, never transforms. The vendor's source schema drives everything: the access
descriptor (`access.layout`) resolves partitions, the format descriptor (`format:`) selects
the parser (JSON arrays/objects, or header-less DSV whose values stay strings; value
interpretation is the engine's job). A declared format the reader cannot honour raises.
Records stream lazily so multi-million-row days never sit in memory at once.

Mirrors the secha-ingestion landing layout: partitions hold `<sha16>.<ext>` payloads plus
`.meta.json` envelope sidecars; multiple payloads in one partition are snapshots, of which
only the latest (by the envelope's `fetched_at`) is read.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from typing import Any

import fsspec

from secha_transform.metadata.loader import MetadataBundle


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


def _read_partition_records(
    fs: Any, directory: str, source_schema: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    path = _latest_payload(fs, directory)
    if path is None:
        return
    with fs.open(path, "rb") as handle:
        # bytes + explicit UTF-8 default: text mode would use the platform encoding
        # (cp1252 on Windows) and misdecode e.g. Finnish characters in the raw payload
        body = handle.read()
    fmt = source_schema.get("format", {})
    fmt_type = fmt.get("type", "json")
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


def read_records(
    landing_root: str, source_schema: dict[str, Any], date: str, meter: str | None = None
) -> Iterator[dict[str, Any]]:
    """Yield one date's raw records, per the source schema's access + format descriptors."""
    fs, base = fsspec.core.url_to_fs(landing_root)
    root = str(base).rstrip("/")
    layout: str = source_schema["access"]["layout"]
    if "{meter}" in layout:
        if meter is None:
            pattern = layout.format(date=date, meter="*")
            partitions = sorted(str(path) for path in fs.glob(f"{root}/{pattern}"))
        else:
            partitions = [f"{root}/{layout.format(date=date, meter=meter)}"]
    else:
        partitions = [f"{root}/{layout.format(date=date)}"]
    for partition in partitions:
        yield from _read_partition_records(fs, partition, source_schema)


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
