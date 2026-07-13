"""The deterministic, vendor-blind transform: raw records + metadata -> canonical rows.

Pure function. No vendor logic; it only interprets the metadata bundle. Wide source records
(shape: wide) are unpivoted via `columns:`/`generated:`; long source records (shape: long) are
resolved via `rows:` keyed on the record's key-field value. The vendor's validation rules are
applied (flag/drop/reject, all counted); same input + same config gives identical output (apart
from the runtime `ingested_at` stamp).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from secha_transform.engine.identity import measurement_id
from secha_transform.engine.models import CanonicalRow, TransformResult, TransformStats
from secha_transform.engine.validation import parse_rules
from secha_transform.metadata.loader import MetadataBundle

_DEFAULT_ID_KEY = [
    "source_vendor",
    "device_id",
    "ts_utc",
    "quantity",
    "phase",
    "variant",
    "harmonic_order",
    "aggregation",
    "source_row_id",
]

# emitted tuple: (quantity, phase, variant, harmonic_order, unit, value, aggregation)
_Emitted = tuple[str, str, str, int | None, str, float, str]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _to_utc(value: Any, datetime_format: str = "iso8601") -> str | None:
    """Normalise a source timestamp to an ISO-8601 UTC string, per the declared format.

    iso8601: naive values get "Z" attached (sources declare UTC); timezone-aware values
    (including negative offsets) pass through; unparseable yields None, never a
    fabricated zone. epoch_ms / epoch_s: integer epochs render as exact UTC instants
    (integer math, so milliseconds can never float-round); unparseable yields None.
    A declared format the engine cannot honour raises.
    """
    if value is None:
        return None
    text = str(value)
    if datetime_format == "iso8601":
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return text if parsed.tzinfo is not None else text + "Z"
    if datetime_format in ("epoch_ms", "epoch_s"):
        try:
            ticks = int(text)
        except ValueError:
            return None
        if datetime_format == "epoch_ms":
            seconds, millis = divmod(ticks, 1000)
            base = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
            return f"{base}.{millis:03d}Z"
        return datetime.fromtimestamp(ticks, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    raise ValueError(f"datetime_format '{datetime_format}' is not implemented by this engine")


def _resolve_factor(factor_field: str, factors: Mapping[str, float]) -> float | None:
    if factor_field == "uk_ik":
        uk, ik = factors.get("uk"), factors.get("ik")
        return None if uk is None or ik is None else uk * ik
    return factors.get(factor_field)


def _apply_transform(col: dict[str, Any], raw: Any, factors: Mapping[str, float]) -> float | None:
    """Apply a column's named transform. Returns None when the value cannot be produced.

    Unimplemented transforms raise: silently falling back to `float(raw)` would let a
    config author use a library-legal rule and get wrong behaviour with no error.
    """
    transform = col.get("transform", "none")
    if transform == "none":
        return float(raw)
    if transform == "scale_by_factor":
        factor = _resolve_factor(col["args"]["factor_field"], factors)
        return None if factor is None else float(raw) * factor
    if transform == "parse_decimal":
        separator = (col.get("args") or {}).get("decimal_separator", ".")
        text = str(raw)
        return float(text.replace(separator, ".")) if separator != "." else float(text)
    raise ValueError(f"transform '{transform}' is not implemented by this engine")


def transform_records(
    records: Iterable[dict[str, Any]],
    bundle: MetadataBundle,
    device_factors: Mapping[str, Mapping[str, float]] | None = None,
) -> TransformResult:
    """Transform raw vendor records into canonical long rows + run stats, per the bundle."""
    factors_by_meter = device_factors or {}
    source = bundle.source_schema
    mapping = bundle.mapping
    rules = parse_rules(bundle.validation)
    stats = TransformStats()

    record_cfg = source.get("record", {})
    meter_field = record_cfg.get("meter_field")  # optional: long sources have no meter concept
    ts_field = record_cfg.get("timestamp_field", "timestamp")
    row_id_field = record_cfg.get("row_id_field", "id")
    default_template = (
        f"{bundle.vendor}:meter:{{{meter_field}}}" if meter_field else f"{bundle.vendor}:device"
    )
    device_template = record_cfg.get("device_id_template", default_template)
    defaults = source.get("defaults", {})
    aggregation = defaults.get("aggregation", "instantaneous")
    interval_s = defaults.get("interval_s")

    shape = source.get("shape", "wide")
    datetime_format = source.get("format", {}).get("datetime_format", "iso8601")
    key_field = record_cfg.get("key_field")
    value_field = record_cfg.get("value_field")
    if shape == "long" and not (key_field and value_field):
        raise ValueError("shape 'long' requires record.key_field and record.value_field")
    rows_by_key = {str(entry["key"]): entry for entry in mapping.get("rows", [])}

    source_dataset = mapping.get("source", "")
    schema_version = str(mapping.get("target_schema_version", "1.0.0"))
    id_key = bundle.target.get("measurement_id_from", _DEFAULT_ID_KEY)
    ingested_at = _now_iso()  # one stamp per run: rows of a batch share their provenance instant

    rows: list[CanonicalRow] = []
    for record in records:
        stats.records_in += 1

        # record-level rules run against the RAW record, before any unpivoting
        record_suspect = False
        record_rejected = False
        for rule in rules.record_rules:
            if rule.passes(record.get(rule.target_field or "")):
                continue
            if rule.on_fail == "flag_suspect":
                record_suspect = True
            else:  # reject_row / drop_row at record level both discard the record
                record_rejected = True
                break
        if record_rejected:
            stats.records_rejected += 1
            continue

        meter = str(record[meter_field]) if meter_field else None
        device_id = device_template.format(**record)
        ts_utc = _to_utc(record.get(ts_field), datetime_format)
        raw_row_id = record.get(row_id_field)
        source_row_id = None if raw_row_id is None else str(raw_row_id)
        factors = factors_by_meter.get(meter, {}) if meter is not None else {}

        emitted: list[_Emitted] = []
        if shape == "long":
            # long source: one record = one reading; the rows table gives it meaning
            entry = rows_by_key.get(str(record.get(key_field)))
            if entry is None:
                stats.records_unmapped += 1  # landed but not (yet) mapped; config decides
                continue
            raw = record.get(value_field)
            if raw is None:
                stats.cells_null_skipped += 1
                continue
            value = _apply_transform(entry, raw, factors)
            if value is not None:
                emitted.append(
                    (
                        entry["quantity"],
                        entry["phase"],
                        entry.get("variant", "none"),
                        entry.get("harmonic_order"),
                        entry["unit"],
                        value,
                        entry.get("aggregation", aggregation),
                    )
                )
        else:
            for col in mapping.get("columns", []):
                raw = record.get(col["src"])
                if raw is None:
                    stats.cells_null_skipped += 1
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
                        aggregation,
                    )
                )
            for gen in mapping.get("generated", []):
                for order in gen["order"]:
                    for idx, phase in gen["phase_map"].items():
                        raw = record.get(gen["pattern"].format(order=order, p=idx))
                        if raw is None:
                            stats.cells_null_skipped += 1
                            continue
                        emitted.append(
                            (
                                gen["quantity"],
                                phase,
                                "none",
                                int(order),
                                gen["unit"],
                                float(raw),
                                aggregation,
                            )
                        )

        # rows are buffered per record so a reject_row can discard the whole record cleanly
        record_rows: list[CanonicalRow] = []
        for quantity, phase, variant, harmonic_order, unit, value, row_aggregation in emitted:
            quality = "suspect" if record_suspect else "ok"
            dropped = False
            for rule in rules.quantity_rules.get(quantity, ()):
                if rule.passes(value):
                    continue
                if rule.on_fail == "flag_suspect":
                    quality = "suspect"
                elif rule.on_fail == "drop_row":
                    dropped = True
                    break
                else:  # reject_row: the whole record is poisoned
                    record_rejected = True
                    break
            if record_rejected:
                break
            if dropped:
                stats.rows_dropped += 1
                continue
            key_values = {
                "source_vendor": bundle.vendor,
                "device_id": device_id,
                "ts_utc": ts_utc,
                "quantity": quantity,
                "phase": phase,
                "variant": variant,
                "harmonic_order": harmonic_order,
                "aggregation": row_aggregation,
                "source_row_id": source_row_id,
            }
            if quality == "suspect":
                stats.rows_suspect += 1
            record_rows.append(
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
                    aggregation=row_aggregation,
                    interval_s=interval_s,
                    quality=quality,
                    source_row_id=source_row_id,
                    schema_version=schema_version,
                    ingested_at=ingested_at,
                )
            )
        if record_rejected:
            stats.records_rejected += 1
            stats.rows_suspect -= sum(1 for r in record_rows if r.quality == "suspect")
            continue
        rows.extend(record_rows)

    stats.rows_emitted = len(rows)
    return TransformResult(rows=rows, stats=stats)
