"""Deterministic surrogate key for a canonical measurement."""

from __future__ import annotations

import hashlib
from typing import Any


def measurement_id(key_fields: list[str], values: dict[str, Any]) -> str:
    """A stable hash of the identity fields, so re-running is idempotent (MERGE-safe)."""
    payload = "|".join(f"{field}={values.get(field)!r}" for field in key_fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
