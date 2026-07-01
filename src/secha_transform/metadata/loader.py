"""Load the secha-metadata rulebook into an in-memory bundle.

The engine depends ONLY on this bundle, never on a vendor name — vendor knowledge is in the config.
Assumed already validated by secha-metadata's `validate.py` (schema + cross-ref + no-collapse); this
loader just reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"expected a YAML mapping in {path}")
    return data


@dataclass(frozen=True)
class MetadataBundle:
    """Everything the engine needs to transform one vendor's data to canonical."""

    vendor: str
    canonical: dict[str, Any]
    vocabulary: dict[str, Any]
    units: dict[str, Any]
    transforms: dict[str, Any]
    target: dict[str, Any]
    source_schema: dict[str, Any]
    mapping: dict[str, Any]
    validation: dict[str, Any]


def load_bundle(metadata_root: str | Path, vendor: str) -> MetadataBundle:
    """Load the canonical layer + one vendor's config from a secha-metadata checkout."""
    root = Path(metadata_root)
    if not (root / "canonical" / "canonical_schema.yaml").exists():
        raise FileNotFoundError(f"secha-metadata not found at {root} (set SECHA_METADATA_ROOT)")
    vendor_dir = root / "vendors" / vendor
    if not vendor_dir.is_dir():
        raise FileNotFoundError(f"vendor '{vendor}' not found at {vendor_dir}")
    return MetadataBundle(
        vendor=vendor,
        canonical=_load(root / "canonical" / "canonical_schema.yaml"),
        vocabulary=_load(root / "canonical" / "quantity_vocabulary.yaml"),
        units=_load(root / "canonical" / "units.yaml"),
        transforms=_load(root / "transforms" / "library.yaml"),
        target=_load(root / "targets" / "canonical.yaml"),
        source_schema=_load(vendor_dir / "source_schema.yaml"),
        mapping=_load(vendor_dir / "mapping.yaml"),
        validation=_load(vendor_dir / "validation.yaml"),
    )
