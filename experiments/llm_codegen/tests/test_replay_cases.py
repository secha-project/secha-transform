"""The Kempower replay's case builder: the registered rules, and refusal of anything else.

Only the golden contract's synthetic records are read here, and only the engine runs on them.
The real inputs are exercised through fakes, to prove that a payload other than the frozen one
is refused before a record is read.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from codegen.replay_cases import (
    EXPORT,
    FROZEN_PARTS,
    _case,
    _golden_records,
    _real_part,
    records_digest,
    refuse_non_finite,
    spread_positions,
)

from secha_transform.metadata.loader import load_bundle


def test_spread_positions_follow_the_registered_rule():
    positions = spread_positions(736_669, 400)
    assert len(positions) == 400 and positions[0] == 0
    assert positions == sorted(set(positions))
    assert positions[-1] == 399 * 736_669 // 400 < 736_669
    assert spread_positions(10, 4) == [0, 2, 5, 7]


def test_a_value_json_cannot_carry_stops_the_build():
    refuse_non_finite([{"soc": 20.0, "year": 2025}], "demo")
    with pytest.raises(ValueError, match="record a deviation"):
        refuse_non_finite([{"soc": 20.0}, {"soc": float("nan")}], "demo")
    with pytest.raises(ValueError):
        records_digest([{"soc": float("inf")}])


def test_the_input_digest_is_stable_and_order_sensitive():
    a, b = {"x": 1, "y": 2.5}, {"x": 2}
    assert records_digest([a, b]) == records_digest([{"y": 2.5, "x": 1}, b])
    assert records_digest([a, b]) != records_digest([b, a])


def test_the_golden_case_is_built_through_the_reader(metadata_root):
    bundle = load_bundle(metadata_root, "kempower")
    records = _golden_records(metadata_root, bundle.source_schema)
    case, facts = _case(metadata_root, bundle, "golden_edge", records, False, "golden")

    assert [r["_payload_position"] for r in records] == [
        f"export=0f1e2d3c/part=00000-c000:{index}" for index in range(6)
    ]
    assert case.case_id == "kempower/test/golden_edge" and not case.real_data
    assert "session" not in case.rulebook["source_schema"]  # as the programs receive it
    assert facts["rows"] == 24 and facts["sessions_full_rulebook"] == 2
    stats = facts["stats"]
    assert (stats["records_in"], stats["records_rejected"]) == (6, 1)
    assert (stats["rows_suspect"], stats["cells_null_skipped"]) == (2, 1)
    assert facts["input_sha256"] == records_digest(case.records)


def _land(landing, source_schema, part, payloads):
    directory = landing / source_schema["access"]["layout"].format(export=EXPORT, part=part)
    directory.mkdir(parents=True)
    for name in payloads:
        pq.write_table(pa.Table.from_pylist([{"soc": 1.0}]), directory / name)


def test_a_part_that_is_not_the_frozen_payload_is_refused(metadata_root, tmp_path):
    source_schema = load_bundle(metadata_root, "kempower").source_schema
    part = "00000-c000"
    name = FROZEN_PARTS[part][0]

    _land(tmp_path / "other", source_schema, part, [name])
    with pytest.raises(ValueError, match="differs from its registered digest"):
        next(_real_part(tmp_path / "other", source_schema, part))

    _land(tmp_path / "two", source_schema, part, [name, "0000000000000000.parquet"])
    with pytest.raises(ValueError, match="payloads"):
        next(_real_part(tmp_path / "two", source_schema, part))

    with pytest.raises(ValueError, match="expected one landed partition"):
        next(_real_part(tmp_path / "none", source_schema, part))
