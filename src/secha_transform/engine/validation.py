"""Apply the vendor's declarative validation rules (the fifth metadata type).

Rule targets:
- record rules (`field:` without `entity:`) run against the RAW record before unpivoting;
  `reject_row` discards the whole record, `flag_suspect` marks all its rows suspect.
- quantity rules (`quantity:`) run against each emitted canonical value; `flag_suspect`
  downgrades quality (value kept), `drop_row` drops the single row, `reject_row` discards
  the whole record.
- the entity rule (measurement.value not_null -> drop) is satisfied by construction: the
  engine never emits null values (null cells are counted, not silently ignored).

A declared rule the engine cannot honour raises; it is never silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_SUPPORTED_TYPES = {"not_null", "range"}
_SUPPORTED_ON_FAIL = {"flag_suspect", "reject_row", "drop_row"}


@dataclass(frozen=True)
class Rule:
    """One parsed validation rule."""

    check_type: str
    on_fail: str
    target_field: str | None = None
    quantity: str | None = None
    minimum: float | None = None
    maximum: float | None = None

    def passes(self, value: Any) -> bool:
        if self.check_type == "not_null":
            return value is not None
        if self.check_type == "range":
            if value is None:  # nullness is not_null's concern, not range's
                return True
            number = float(value)
            if self.minimum is not None and number < self.minimum:
                return False
            return not (self.maximum is not None and number > self.maximum)
        raise ValueError(f"validation rule type '{self.check_type}' is not implemented")


@dataclass(frozen=True)
class ParsedRules:
    """Vendor rules split by target, ready for the transform loop."""

    record_rules: tuple[Rule, ...] = ()
    quantity_rules: dict[str, tuple[Rule, ...]] = field(default_factory=dict)


def parse_rules(validation: dict[str, Any]) -> ParsedRules:
    """Parse validation.yaml content; raise on anything the engine cannot honour."""
    record_rules: list[Rule] = []
    quantity_rules: dict[str, list[Rule]] = {}
    for raw in (validation or {}).get("rules", []):
        check_type = raw["type"]
        on_fail = raw["on_fail"]
        if raw.get("entity") == "measurement":
            # value not_null is enforced by construction (nulls never emit)
            if raw.get("field") == "value" and check_type == "not_null":
                continue
            raise ValueError(f"entity-level validation rule not implemented: {raw}")
        if check_type not in _SUPPORTED_TYPES:
            raise ValueError(f"validation rule type '{check_type}' is not implemented")
        if on_fail not in _SUPPORTED_ON_FAIL:
            raise ValueError(f"validation on_fail '{on_fail}' is not implemented")
        rule = Rule(
            check_type=check_type,
            on_fail=on_fail,
            target_field=raw.get("field"),
            quantity=raw.get("quantity"),
            minimum=raw.get("min"),
            maximum=raw.get("max"),
        )
        if rule.quantity is not None:
            quantity_rules.setdefault(rule.quantity, []).append(rule)
        elif rule.target_field is not None:
            record_rules.append(rule)
        else:
            raise ValueError(f"validation rule needs a field, quantity, or entity target: {raw}")
    return ParsedRules(
        record_rules=tuple(record_rules),
        quantity_rules={q: tuple(rules) for q, rules in quantity_rules.items()},
    )
