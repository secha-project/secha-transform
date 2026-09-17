"""Reference implementation of CONTRACT.md, standard library only.

This program exists to validate the harness, not to replace the engine. If the harness
scores it below 100% on any case, then the contract, the cases or the comparison is wrong,
and no model result can be trusted until that is fixed. Assigning RULEBOOK turns it into a
frozen per-vendor program, which the drift cases must catch, and the deliberate mutants
defined in harness.py must be caught as well.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from typing import Any

RULEBOOK: dict[str, Any] | None = None

STATS = (
    "records_in",
    "records_rejected",
    "records_unmapped",
    "rows_emitted",
    "rows_suspect",
    "rows_dropped",
    "cells_null_skipped",
)
ON_FAIL = ("flag_suspect", "reject_row", "drop_row")


def to_utc(value: Any, datetime_format: str) -> str | None:
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
    raise ValueError(f"datetime_format '{datetime_format}' is not supported")


def passes(rule: dict[str, Any], value: Any) -> bool:
    if rule["type"] == "not_null":
        return value is not None
    if value is None:
        return True
    number = float(value)
    if rule.get("min") is not None and number < rule["min"]:
        return False
    return not (rule.get("max") is not None and number > rule["max"])


def apply_transform(entry: dict[str, Any], raw: Any, factors: dict[str, float]) -> float | None:
    transform = entry.get("transform", "none")
    if transform == "none":
        return float(raw)
    if transform == "scale_by_factor":
        name = entry["args"]["factor_field"]
        if name == "uk_ik":
            uk, ik = factors.get("uk"), factors.get("ik")
            factor = None if uk is None or ik is None else uk * ik
        else:
            factor = factors.get(name)
        return None if factor is None else float(raw) * factor
    if transform == "parse_decimal":
        separator = (entry.get("args") or {}).get("decimal_separator", ".")
        text = str(raw)
        return float(text.replace(separator, ".")) if separator != "." else float(text)
    raise ValueError(f"transform '{transform}' is not supported")


def split_rules(
    validation: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    record_rules: list[dict[str, Any]] = []
    quantity_rules: dict[str, list[dict[str, Any]]] = {}
    for rule in (validation or {}).get("rules", []):
        if rule.get("entity") == "measurement":
            if rule.get("field") == "value" and rule["type"] == "not_null":
                continue
            raise ValueError(f"entity rule not supported: {rule}")
        if rule["type"] not in ("not_null", "range") or rule["on_fail"] not in ON_FAIL:
            raise ValueError(f"rule not supported: {rule}")
        if rule.get("quantity") is not None:
            quantity_rules.setdefault(rule["quantity"], []).append(rule)
        elif rule.get("field") is not None:
            record_rules.append(rule)
        else:
            raise ValueError(f"rule has no target: {rule}")
    return record_rules, quantity_rules


def identity(fields: list[str], values: dict[str, Any]) -> str:
    payload = "|".join(f"{name}={values.get(name)!r}" for name in fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def transform(payload: dict[str, Any]) -> dict[str, Any]:
    rulebook = payload.get("rulebook") or RULEBOOK
    if rulebook is None:
        raise SystemExit("no rulebook: pass one on standard input or assign RULEBOOK")
    vendor = rulebook["vendor"]
    source = rulebook["source_schema"]
    mapping = rulebook["mapping"]
    id_fields = rulebook["target"]["measurement_id_from"]
    record_rules, quantity_rules = split_rules(rulebook["validation"])
    factors_by_meter = payload.get("device_factors") or {}

    record_cfg = source.get("record", {})
    meter_field = record_cfg.get("meter_field")
    ts_field = record_cfg.get("timestamp_field", "timestamp")
    row_id_field = record_cfg.get("row_id_field", "id")
    default_template = f"{vendor}:meter:{{{meter_field}}}" if meter_field else f"{vendor}:device"
    template = record_cfg.get("device_id_template", default_template)
    defaults = source.get("defaults", {})
    default_aggregation = defaults.get("aggregation", "instantaneous")
    interval_s = defaults.get("interval_s")
    shape = source.get("shape", "wide")
    datetime_format = source.get("format", {}).get("datetime_format", "iso8601")
    key_field = record_cfg.get("key_field")
    value_field = record_cfg.get("value_field")
    rows_by_key = {str(entry["key"]): entry for entry in mapping.get("rows", [])}
    dataset = mapping.get("source", "")
    schema_version = str(mapping.get("target_schema_version", "1.0.0"))

    stats = dict.fromkeys(STATS, 0)
    output: list[dict[str, Any]] = []
    for record in payload["records"]:
        stats["records_in"] += 1
        suspect = False
        rejected = False
        for rule in record_rules:
            if passes(rule, record.get(rule["field"])):
                continue
            if rule["on_fail"] == "flag_suspect":
                suspect = True
            else:
                rejected = True
                break
        if rejected:
            stats["records_rejected"] += 1
            continue

        meter = str(record[meter_field]) if meter_field else None
        device_id = template.format(**record)
        ts_utc = to_utc(record.get(ts_field), datetime_format)
        raw_row_id = record.get(row_id_field)
        source_row_id = None if raw_row_id is None else str(raw_row_id)
        factors = factors_by_meter.get(meter, {}) if meter is not None else {}

        candidates: list[tuple[str, str, str, int | None, str, float, str]] = []
        if shape == "long":
            entry = rows_by_key.get(str(record.get(key_field)))
            if entry is None:
                stats["records_unmapped"] += 1
                continue
            raw = record.get(value_field)
            if raw is None:
                stats["cells_null_skipped"] += 1
                continue
            value = apply_transform(entry, raw, factors)
            if value is not None:
                candidates.append(
                    (
                        entry["quantity"],
                        entry["phase"],
                        entry.get("variant", "none"),
                        entry.get("harmonic_order"),
                        entry["unit"],
                        value,
                        entry.get("aggregation", default_aggregation),
                    )
                )
        else:
            for column in mapping.get("columns", []):
                raw = record.get(column["src"])
                if raw is None:
                    stats["cells_null_skipped"] += 1
                    continue
                value = apply_transform(column, raw, factors)
                if value is None:
                    continue
                candidates.append(
                    (
                        column["quantity"],
                        column["phase"],
                        column.get("variant", "none"),
                        None,
                        column["unit"],
                        value,
                        default_aggregation,
                    )
                )
            for rule in mapping.get("generated", []):
                for order in rule["order"]:
                    for position, phase in rule["phase_map"].items():
                        raw = record.get(rule["pattern"].format(order=order, p=position))
                        if raw is None:
                            stats["cells_null_skipped"] += 1
                            continue
                        candidates.append(
                            (
                                rule["quantity"],
                                phase,
                                "none",
                                int(order),
                                rule["unit"],
                                float(raw),
                                default_aggregation,
                            )
                        )

        record_rows: list[dict[str, Any]] = []
        for quantity, phase, variant, harmonic_order, unit, value, aggregation in candidates:
            quality = "suspect" if suspect else "ok"
            dropped = False
            for rule in quantity_rules.get(quantity, []):
                if passes(rule, value):
                    continue
                if rule["on_fail"] == "flag_suspect":
                    quality = "suspect"
                elif rule["on_fail"] == "drop_row":
                    dropped = True
                    break
                else:
                    rejected = True
                    break
            if rejected:
                break
            if dropped:
                stats["rows_dropped"] += 1
                continue
            row = {
                "source_vendor": vendor,
                "source_dataset": dataset,
                "device_id": device_id,
                "ts_utc": ts_utc,
                "quantity": quantity,
                "phase": phase,
                "variant": variant,
                "harmonic_order": harmonic_order,
                "value": value,
                "unit": unit,
                "aggregation": aggregation,
                "interval_s": interval_s,
                "quality": quality,
                "source_row_id": source_row_id,
                "schema_version": schema_version,
            }
            row["measurement_id"] = identity(id_fields, row)
            record_rows.append(row)
        if rejected:
            stats["records_rejected"] += 1
            continue
        output.extend(record_rows)

    stats["rows_emitted"] = len(output)
    stats["rows_suspect"] = sum(1 for row in output if row["quality"] == "suspect")
    return {"rows": output, "stats": stats}


def main() -> None:
    json.dump(transform(json.load(sys.stdin)), sys.stdout)


if __name__ == "__main__":
    main()
