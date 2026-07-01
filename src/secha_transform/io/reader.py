"""Read raw records and device factors from the landing zone (fsspec).

Reads only — never transforms. Mirrors the secha-ingestion landing layout:
    <root>/vendor=<v>/source=<s>/date=<d>/[meter=<m>/]<sha>.json (+ .meta.json sidecar)
"""

from __future__ import annotations

import json
from typing import Any

import fsspec

from secha_transform.metadata.loader import MetadataBundle


def _read_json_records(fs: Any, directory: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not fs.exists(directory):
        return records
    # NOTE (Phase 1): reads all snapshots; latest_by_fetched_at selection is TODO.
    for path in sorted(fs.glob(f"{directory}/*.json")):
        if path.endswith(".meta.json"):
            continue
        with fs.open(path, "r") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            records.extend(data)
        elif isinstance(data, dict):
            records.append(data)
    return records


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
