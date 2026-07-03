"""Read raw records and device factors from the landing zone (fsspec).

Reads only — never transforms. Mirrors the secha-ingestion landing layout:
    <root>/vendor=<v>/source=<s>/date=<d>/[meter=<m>/]<sha>.json (+ .meta.json sidecar)
"""

from __future__ import annotations

import json
from typing import Any

import fsspec

from secha_transform.metadata.loader import MetadataBundle


def _latest_payload(fs: Any, directory: str) -> str | None:
    """Pick the newest landed snapshot in a partition.

    Ingestion keeps every changed snapshot side by side (immutability); reading them all
    would duplicate — or worse, contradict — records. Recency comes from the envelope
    sidecar's `fetched_at` (path as a deterministic tie-breaker), which implements the
    source schema's `snapshot_selection: latest_by_fetched_at`.
    """
    if not fs.exists(directory):
        return None
    paths: list[str] = [
        str(p) for p in fs.glob(f"{directory}/*.json") if not str(p).endswith(".meta.json")
    ]
    if not paths:
        return None

    def recency(path: str) -> tuple[str, str]:
        meta_path = path[: -len(".json")] + ".meta.json"
        fetched_at = ""
        if fs.exists(meta_path):
            with fs.open(meta_path, "rb") as handle:
                fetched_at = str(json.loads(handle.read()).get("fetched_at") or "")
        return (fetched_at, path)

    return max(paths, key=recency)


def _read_json_records(fs: Any, directory: str) -> list[dict[str, Any]]:
    path = _latest_payload(fs, directory)
    if path is None:
        return []
    with fs.open(path, "rb") as handle:
        # bytes + explicit UTF-8: text mode would use the platform encoding (cp1252 on
        # Windows) and misdecode e.g. Finnish characters in the raw payload
        data = json.loads(handle.read().decode("utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def read_measurements(
    landing_root: str, vendor: str, date: str, meter: str | None = None
) -> list[dict[str, Any]]:
    """Read the raw `/measurements/` records for a vendor/date (optionally one meter)."""
    fs, base = fsspec.core.url_to_fs(landing_root)
    src = f"{str(base).rstrip('/')}/vendor={vendor}/source=measurements/date={date}"
    partitions = [f"{src}/meter={meter}"] if meter else sorted(fs.glob(f"{src}/meter=*"))
    records: list[dict[str, Any]] = []
    for partition in partitions:
        records.extend(_read_json_records(fs, partition))
    return records


def read_device_factors(
    landing_root: str, vendor: str, date: str, bundle: MetadataBundle
) -> dict[str, dict[str, float]]:
    """Build {meter_id: {"uk": ..., "ik": ...}} from the raw `/meters/` records.

    The device-record field names are taken from the vendor's `source_schema.device_factors`,
    keeping this vendor-blind.
    """
    fs, base = fsspec.core.url_to_fs(landing_root)
    meters_dir = f"{str(base).rstrip('/')}/vendor={vendor}/source=meters/date={date}"
    devices = _read_json_records(fs, meters_dir)
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
