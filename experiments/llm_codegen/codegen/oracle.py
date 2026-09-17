"""The engine as oracle: run `secha-transform` on exactly the input a program receives.

A rulebook here is the part of a vendor directory that decides canonical output, held as
plain JSON-compatible data. The same object is shown to a model, passed to a program in
interpreter mode, edited for drift cases, and handed to the engine, so the program and the
oracle can never be working from different configurations.
"""

from __future__ import annotations

import copy
import dataclasses
from functools import lru_cache
from pathlib import Path
from typing import Any

from secha_transform.engine.transform import transform_records
from secha_transform.metadata.loader import MetadataBundle, load_bundle

# Filled at run time or reserved for sources this study does not cover; never scored.
RUNTIME_FIELDS = ("ingested_at", "location_id", "session_id", "ts_session_offset_s")

# Source schema keys that change what the engine emits. Ownership, access layout and field
# descriptions describe a system, not a transformation, so they stay out of prompts.
SOURCE_KEYS = ("vendor", "source", "shape", "format", "record", "defaults", "device_factors")


def rulebook_from_bundle(bundle: MetadataBundle) -> dict[str, Any]:
    """The transformation-relevant rulebook of one vendor, as JSON-compatible data."""
    source = {
        key: copy.deepcopy(bundle.source_schema[key])
        for key in SOURCE_KEYS
        if key in bundle.source_schema
    }
    source["fields"] = [entry["name"] for entry in bundle.source_schema.get("fields", [])]
    return {
        "vendor": bundle.vendor,
        "source_schema": source,
        "mapping": copy.deepcopy(bundle.mapping),
        "validation": copy.deepcopy(bundle.validation),
        "target": {"measurement_id_from": list(bundle.target["measurement_id_from"])},
    }


@lru_cache(maxsize=8)
def _base_bundle(metadata_root: str, vendor: str) -> MetadataBundle:
    return load_bundle(metadata_root, vendor)


def run_engine(
    metadata_root: Path,
    rulebook: dict[str, Any],
    records: list[dict[str, Any]],
    device_factors: dict[str, dict[str, float]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Canonical rows and run statistics from the engine, for this rulebook and input."""
    base = _base_bundle(str(metadata_root), rulebook["vendor"])
    bundle = dataclasses.replace(
        base,
        source_schema=copy.deepcopy(rulebook["source_schema"]),
        mapping=copy.deepcopy(rulebook["mapping"]),
        validation=copy.deepcopy(rulebook["validation"]),
        target={
            **base.target,
            "measurement_id_from": list(rulebook["target"]["measurement_id_from"]),
        },
    )
    result = transform_records(copy.deepcopy(records), bundle, copy.deepcopy(device_factors))
    rows = [
        {key: value for key, value in row.to_dict().items() if key not in RUNTIME_FIELDS}
        for row in result.rows
    ]
    return rows, dataclasses.asdict(result.stats)
