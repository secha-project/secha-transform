"""Write canonical rows.

Phase 1: local **parquet** dataset, Hive-partitioned by `source_vendor` + `event_date`, the same
columnar form Delta stores underneath. Phase 3 swaps this for a Delta/Unity-Catalog MERGE on the
TUNI Spark Connect cluster (the `spark` extra), keyed on `measurement_id`.

Idempotency (Phase 1): part files are named after the run scope (`run_tag`), and existing files
are overwritten (`overwrite_or_ignore`). Re-running the same scope replaces its own output;
other scopes (e.g. another meter sharing the same date partition) are untouched. A blunt
`delete_matching` would be unsafe here because partitions are vendor+date, not per-meter.
Caveat: if a re-run produces fewer part files than before, stale higher-numbered parts remain;
acceptable at slice sizes (one file per scope), resolved for real by the Phase-3 MERGE.
"""

from __future__ import annotations

import re

import pyarrow as pa
import pyarrow.parquet as pq

from secha_transform.engine.models import CanonicalRow

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def write_canonical_parquet(
    rows: list[CanonicalRow], canonical_root: str, run_tag: str = "run"
) -> str:
    """Write canonical rows to a partitioned parquet dataset; return the dataset root."""
    if not rows:
        return canonical_root
    records = []
    for row in rows:
        record = row.to_dict()
        record["event_date"] = (row.ts_utc or "")[:10] or "unknown"
        records.append(record)
    table = pa.Table.from_pylist(records)
    safe_tag = _UNSAFE.sub("-", run_tag)
    pq.write_to_dataset(
        table,
        root_path=canonical_root,
        partition_cols=["source_vendor", "event_date"],
        basename_template=f"{safe_tag}-{{i}}.parquet",
        existing_data_behavior="overwrite_or_ignore",
    )
    return canonical_root
