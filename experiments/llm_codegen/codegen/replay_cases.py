"""The Kempower replay's inputs, built exactly as kempower_replay/PROTOCOL.md registers them.

Three cases of the test split: the golden contract's six synthetic records, landed as one
Parquet payload and read back through the reader, and two samples of real landed parts chosen
by a fixed rule. A record reaches a program as the engine receives it from the reader, its
position stamp included. The real payloads are checked against their registered digests and
record counts before a record is read, so a case can only be built from the frozen inputs.

The engine's output comes from the unchanged oracle, which runs the engine on the rulebook the
programs receive, without the session block. The builder also runs the engine on the full
rulebook and requires the same scored fields and statistics; the session counts come from
that run.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from codegen.cases import Case
from codegen.oracle import RUNTIME_FIELDS, rulebook_from_bundle, run_engine
from secha_transform.engine.transform import transform_records
from secha_transform.io.reader import iter_partitions, read_partition
from secha_transform.metadata.loader import MetadataBundle, load_bundle

VENDOR = "kempower"
EXPORT = "95d29330"
FIXTURE = Path("tests") / "fixtures" / "kempower" / "raw_records_sample.json"
FIXTURE_SHA256 = "752280103c6d75752499144aeebe94a27627303aacdcd444d5869c748a74d65d"
# part: (payload file, SHA-256, records), as registered
FROZEN_PARTS: dict[str, tuple[str, str, int]] = {
    "00000-c000": (
        "cde78d3fe63ad483.parquet",
        "cde78d3fe63ad483626b7fba0589b658ea3103981117f4363aa3fbc45d50e3c4",
        736_002,
    ),
    "00049-c000": (
        "1c1b7b2a504e26b5.parquet",
        "1c1b7b2a504e26b56cbf581a580323ddc628f2e3172da4418f7c53b57d6f32f7",
        736_669,
    ),
}
START_PART, START_RECORDS = "00000-c000", 200
SPREAD_PART, SPREAD_RECORDS = "00049-c000", 400
GOLDEN_PAYLOAD = "0f1e2d3c4b5a6978.parquet"  # the name the golden test lands it under


def spread_positions(total: int, count: int) -> list[int]:
    """floor(i * total / count) for i from 0 to count - 1: evenly spaced, the first included."""
    return [index * total // count for index in range(count)]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def records_digest(records: list[dict[str, Any]]) -> str:
    """A digest of a case's input as a program receives it (canonical JSON)."""
    text = json.dumps(records, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def refuse_non_finite(records: list[dict[str, Any]], case: str) -> None:
    """Stop on a value JSON cannot carry exactly; the protocol records a deviation instead."""
    for index, record in enumerate(records):
        for field, value in record.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(
                    f"{case}: record {index}, field {field!r} is {value}, which JSON cannot "
                    "carry exactly; stop and record a deviation (PROTOCOL.md, Inputs)"
                )


def _canonical(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(row, sort_keys=True) for row in rows)


def _golden_records(metadata_root: Path, source_schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Land the golden sample as the ingestion layer would, then read it back."""
    fixture = metadata_root / FIXTURE
    if sha256_file(fixture) != FIXTURE_SHA256:
        raise ValueError(f"{fixture} differs from the registered golden fixture")
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="secha-replay-") as landing:
        partition = Path(landing) / source_schema["access"]["layout"].format(**raw["partition"])
        partition.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist(raw["records"]), partition / GOLDEN_PAYLOAD)
        envelope = json.dumps({"fetched_at": "2026-09-24T00:00:00Z"})
        (partition / f"{Path(GOLDEN_PAYLOAD).stem}.meta.json").write_text(envelope, "utf-8")
        (landed,) = list(iter_partitions(landing, source_schema))
        return list(read_partition(landing, landed, source_schema))


def _real_part(
    landing_root: Path, source_schema: dict[str, Any], part: str
) -> Iterator[dict[str, Any]]:
    """A frozen part's records, in payload order, once its payload is proven to be the frozen
    one: the only payload in its partition, with the registered digest and record count."""
    name, digest, total = FROZEN_PARTS[part]
    partitions = list(iter_partitions(str(landing_root), source_schema, export=EXPORT, part=part))
    if len(partitions) != 1:
        raise ValueError(f"part {part}: expected one landed partition, found {len(partitions)}")
    directory = Path(partitions[0].directory)
    payloads = sorted(
        p.name
        for p in directory.iterdir()
        if p.is_file() and not p.name.endswith((".meta.json", ".tmp"))
    )
    if payloads != [name]:
        raise ValueError(f"part {part}: payloads {payloads}, registered [{name!r}]")
    if sha256_file(directory / name) != digest:
        raise ValueError(f"part {part}: {name} differs from its registered digest")
    if pq.ParquetFile(directory / name).metadata.num_rows != total:
        raise ValueError(f"part {part}: {name} does not hold the registered {total} records")
    return read_partition(str(landing_root), partitions[0], source_schema)


def _case(
    metadata_root: Path,
    bundle: MetadataBundle,
    name: str,
    records: list[dict[str, Any]],
    real_data: bool,
    description: str,
) -> tuple[Case, dict[str, Any]]:
    refuse_non_finite(records, name)
    rulebook = rulebook_from_bundle(bundle)
    rows, stats = run_engine(metadata_root, rulebook, records, {})

    # The oracle leaves the session block out, as the programs see it; the full rulebook must
    # give the same scored fields and statistics, and it gives the sessions reported for scale.
    full = transform_records(copy.deepcopy(records), bundle)
    full_rows = [
        {key: value for key, value in row.to_dict().items() if key not in RUNTIME_FIELDS}
        for row in full.rows
    ]
    full_stats = {key: getattr(full.stats, key) for key in stats}
    if (_canonical(rows), stats) != (_canonical(full_rows), full_stats):
        raise ValueError(f"{name}: the session block changes a scored field or a statistic")

    case = Case(
        vendor=VENDOR,
        name=name,
        split="test",
        records=copy.deepcopy(records),
        device_factors={},
        rulebook=rulebook,
        expected_rows=rows,
        expected_stats=stats,
        real_data=real_data,
        description=description,
    )
    facts = {
        "case": case.case_id,
        "records": len(records),
        "input_sha256": records_digest(records),
        "rows": len(rows),
        "stats": stats,
        "sessions_full_rulebook": len(full.sessions),
        "real_data": real_data,
    }
    return case, facts


def build_replay_cases(
    metadata_root: Path, landing_root: Path
) -> tuple[list[Case], list[dict[str, Any]]]:
    """The three registered cases, and the facts the case manifest records about each."""
    bundle = load_bundle(metadata_root, VENDOR)
    source_schema = bundle.source_schema

    start = list(
        itertools.islice(_real_part(landing_root, source_schema, START_PART), START_RECORDS)
    )
    total = FROZEN_PARTS[SPREAD_PART][2]
    wanted = set(spread_positions(total, SPREAD_RECORDS))
    spread = [
        record
        for index, record in enumerate(_real_part(landing_root, source_schema, SPREAD_PART))
        if index in wanted
    ]
    if (len(start), len(spread)) != (START_RECORDS, SPREAD_RECORDS):
        raise ValueError(f"sampled {len(start)} and {len(spread)} records, not the registered")

    built = [
        _case(
            metadata_root,
            bundle,
            "golden_edge",
            _golden_records(metadata_root, source_schema),
            real_data=False,
            description=(
                "The golden contract's six synthetic records, landed as Parquet and read back: "
                "two readings at one session offset, a missing temperature, a state of charge "
                "above 100, a negative voltage and a record without a session."
            ),
        ),
        _case(
            metadata_root,
            bundle,
            "real_part_start",
            start,
            real_data=True,
            description=f"The first {START_RECORDS} records of part {START_PART}.",
        ),
        _case(
            metadata_root,
            bundle,
            "real_part_spread",
            spread,
            real_data=True,
            description=(
                f"{SPREAD_RECORDS} records of part {SPREAD_PART} at positions "
                f"floor(i * {total} / {SPREAD_RECORDS})."
            ),
        ),
    ]
    return [case for case, _ in built], [facts for _, facts in built]
