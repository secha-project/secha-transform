"""Write canonical rows.

Phase 1: local **parquet** dataset, Hive-partitioned by `source_vendor` + `event_date`, the same
columnar form Delta stores underneath. Phase 3 swaps this for a Delta/Unity-Catalog MERGE on the
TUNI Spark Connect cluster (the `spark` extra), keyed on `measurement_id`.

Every file carries the same explicit schema, derived from `CanonicalRow`, rather than types
inferred from the values in one batch. A batch from a source without clock time has `ts_utc`
null throughout, and inference would type it `null` in that file and `string` in another,
which a reader of the whole dataset cannot reconcile. For the same reason `event_date` is a
real null for such a source, stored in Hive's default partition (`__HIVE_DEFAULT_PARTITION__`,
which Spark and pyarrow both read back as null), never a placeholder string that the DATE
column of the Delta table would reject.

Charging-session rows (and any other canonical entity) are written the same way, to their own
dataset, typed from the canonical schema's field list.

Idempotency (Phase 1): part files are named after the run scope (`run_tag`), and existing files
are overwritten (`overwrite_or_ignore`). Re-running the same scope replaces its own output;
other scopes (e.g. another meter sharing the same date partition) are untouched. A blunt
`delete_matching` would be unsafe here because partitions are vendor+date, not per-meter.
Caveat: if a re-run produces fewer part files than before, stale higher-numbered parts remain;
acceptable at slice sizes (one file per scope), resolved for real by the Phase-3 MERGE.
"""

from __future__ import annotations

import re
from dataclasses import fields
from typing import Any

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from secha_transform.engine.models import CANONICAL_ROW_FIELDS, CanonicalRow

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_PYTHON_TO_ARROW = {"str": pa.string(), "int": pa.int64(), "float": pa.float64()}


def _canonical_arrow_type(field_type: str) -> pa.DataType:
    """Arrow type for a canonical schema type. Timestamps stay ISO strings, as the engine
    writes them; the Delta sink casts on load. Unknown types raise, never guess."""
    if field_type == "string" or field_type.startswith(("enum:", "vocab:", "registry:")):
        return pa.string()
    if field_type in ("int", "long"):
        return pa.int64()
    if field_type == "double":
        return pa.float64()
    if field_type == "timestamp":
        return pa.string()
    raise ValueError(f"canonical field type '{field_type}' has no Arrow mapping")


def _measurement_schema() -> pa.Schema:
    columns = [
        (field.name, _PYTHON_TO_ARROW[str(field.type).replace(" | None", "")])
        for field in fields(CanonicalRow)
    ]
    return pa.schema([*columns, ("event_date", pa.string())])


_MEASUREMENT_SCHEMA = _measurement_schema()

# How to READ these datasets. Declared, not inferred: a dataset that holds only sources
# without clock time has no non-null event_date for pyarrow to infer a type from, and
# inference then fails outright ("no non-null segments").
MEASUREMENT_PARTITIONING = ds.partitioning(
    pa.schema([("source_vendor", pa.string()), ("event_date", pa.string())]), flavor="hive"
)
ENTITY_PARTITIONING = ds.partitioning(pa.schema([("source_vendor", pa.string())]), flavor="hive")


def _write(table: pa.Table, root: str, partition_cols: list[str], run_tag: str) -> None:
    pq.write_to_dataset(
        table,
        root_path=root,
        partition_cols=partition_cols,
        basename_template=f"{_UNSAFE.sub('-', run_tag)}-{{i}}.parquet",
        existing_data_behavior="overwrite_or_ignore",
    )


def write_canonical_parquet(
    rows: list[CanonicalRow], canonical_root: str, run_tag: str = "run"
) -> str:
    """Write canonical rows to a partitioned parquet dataset; return the dataset root."""
    if not rows:
        return canonical_root
    # built column by column: no per-row dict, which dominated large runs
    columns: dict[str, list[Any]] = {
        name: [getattr(row, name) for row in rows] for name in CANONICAL_ROW_FIELDS
    }
    columns["event_date"] = [row.ts_utc[:10] if row.ts_utc else None for row in rows]
    table = pa.Table.from_pydict(columns, schema=_MEASUREMENT_SCHEMA)
    _write(table, canonical_root, ["source_vendor", "event_date"], run_tag)
    return canonical_root


def write_entity_parquet(
    rows: list[dict[str, Any]],
    entity_root: str,
    field_types: list[tuple[str, str]],
    run_tag: str = "run",
) -> str:
    """Write rows of a canonical entity (e.g. charging_session), partitioned by vendor.

    `field_types` is the entity's (name, canonical type) list from the canonical schema, so
    the files are typed by the schema, not by whichever values one batch happened to hold.
    """
    if not rows:
        return entity_root
    schema = pa.schema([(name, _canonical_arrow_type(kind)) for name, kind in field_types])
    table = pa.Table.from_pylist(rows, schema=schema)
    _write(table, entity_root, ["source_vendor"], run_tag)
    return entity_root
