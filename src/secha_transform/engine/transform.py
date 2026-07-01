"""The deterministic, vendor-blind transform: raw records + metadata -> canonical rows.

Pure function. No vendor logic — it only interprets the metadata bundle. A wide source record is
unpivoted into long canonical rows (one per measured quantity); same input + same config gives
identical output (apart from the runtime `ingested_at` stamp).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from secha_transform.engine.identity import measurement_id
from secha_transform.engine.models import CanonicalRow
from secha_transform.metadata.loader import MetadataBundle

_DEFAULT_ID_KEY = [
    "source_vendor",
    "device_id",
    "ts_utc",
    "quantity",
    "phase",
    "variant",
    "harmonic_order",
    "source_row_id",
]

# emitted tuple: (quantity, phase, variant, harmonic_order, unit, value)
_Emitted = tuple[str, str, str, int | None, str, float]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _to_utc(value: Any) -> str | None:
    """Attach UTC to a naive ISO timestamp (source timestamps are UTC; raw has no marker)."""
    if value is None:
        return None
    text = str(value)
    return text if (text.endswith("Z") or "+" in text) else text + "Z"


def _resolve_factor(factor_field: str, factors: Mapping[str, float]) -> float | None:
    if factor_field == "uk_ik":
        uk, ik = factors.get("uk"), factors.get("ik")
        return None if uk is None or ik is None else uk * ik
    return factors.get(factor_field)


def _apply_transform(col: dict[str, Any], raw: Any, factors: Mapping[str, float]) -> float | None:
    """Apply a column's named transform. Returns None when the value cannot be produced."""
    transform = col.get("transform", "none")
    if transform == "scale_by_factor":
        factor = _resolve_factor(col["args"]["factor_field"], factors)
        return None if factor is None else float(raw) * factor
    # "none" (and other value-preserving rules): canonical values are numeric.
    return float(raw)


def transform_records(
    records: list[dict[str, Any]],
    bundle: MetadataBundle,
    device_factors: Mapping[str, Mapping[str, float]] | None = None,
) -> list[CanonicalRow]:
    """Transform raw vendor records into canonical long-format rows, per the metadata bundle."""
    factors_by_meter = device_factors or {}
    source = bundle.source_schema
    mapping = bundle.mapping

    record_cfg = source.get("record", {})
    meter_field = record_cfg.get("meter_field", "meter")
    ts_field = record_cfg.get("timestamp_field", "timestamp")
    row_id_field = record_cfg.get("row_id_field", "id")
    device_template = record_cfg.get(
        "device_id_template", bundle.vendor + ":meter:{" + meter_field + "}"
    )
    defaults = source.get("defaults", {})
    aggregation = defaults.get("aggregation", "instantaneous")
    interval_s = defaults.get("interval_s")

    source_dataset = mapping.get("source", "")
    schema_version = str(mapping.get("target_schema_version", "1.0.0"))
    id_key = bundle.target.get("measurement_id_from", _DEFAULT_ID_KEY)

    rows: list[CanonicalRow] = []
    for record in records:
        meter = str(record[meter_field])
        device_id = device_template.format(**record)
        ts_utc = _to_utc(record.get(ts_field))
        raw_row_id = record.get(row_id_field)
        source_row_id = None if raw_row_id is None else str(raw_row_id)
        factors = factors_by_meter.get(meter, {})

        emitted: list[_Emitted] = []
        for col in mapping.get("columns", []):
            raw = record.get(col["src"])
            if raw is None:
                continue
            value = _apply_transform(col, raw, factors)
            if value is None:  # e.g. a scaling factor is unavailable for this device
                continue
            emitted.append(
                (
                    col["quantity"],
                    col["phase"],
                    col.get("variant", "none"),
                    None,
                    col["unit"],
                    value,
                )
            )
        for gen in mapping.get("generated", []):
            for order in gen["order"]:
                for idx, phase in gen["phase_map"].items():
                    raw = record.get(gen["pattern"].format(order=order, p=idx))
                    if raw is None:
                        continue
                    emitted.append(
                        (gen["quantity"], phase, "none", int(order), gen["unit"], float(raw))
                    )

        for quantity, phase, variant, harmonic_order, unit, value in emitted:
            key_values = {
                "source_vendor": bundle.vendor,
                "device_id": device_id,
                "ts_utc": ts_utc,
                "quantity": quantity,
                "phase": phase,
                "variant": variant,
                "harmonic_order": harmonic_order,
                "source_row_id": source_row_id,
            }
            rows.append(
                CanonicalRow(
                    measurement_id=measurement_id(id_key, key_values),
                    source_vendor=bundle.vendor,
                    source_dataset=source_dataset,
                    device_id=device_id,
                    ts_utc=ts_utc,
                    quantity=quantity,
                    phase=phase,
                    variant=variant,
                    harmonic_order=harmonic_order,
                    value=value,
                    unit=unit,
                    aggregation=aggregation,
                    interval_s=interval_s,
                    quality="ok",
                    source_row_id=source_row_id,
                    schema_version=schema_version,
                    ingested_at=_now_iso(),
                )
            )
    return rows
