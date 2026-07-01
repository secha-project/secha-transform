"""Write canonical rows.

Phase 1: local **parquet** dataset, Hive-partitioned by `source_vendor` + `event_date` — the same
columnar form Delta stores underneath. Phase 3 swaps this for a Delta/Unity-Catalog MERGE on the
TUNI Spark Connect cluster (the `spark` extra), keyed on `measurement_id`.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq

from secha_transform.engine.models import CanonicalRow


def write_canonical_parquet(rows: list[CanonicalRow], canonical_root: str) -> str:
    """Write canonical rows to a partitioned parquet dataset; return the dataset root."""
    if not rows:
        return canonical_root
    records = []
    for row in rows:
        record = row.to_dict()
        record["event_date"] = (row.ts_utc or "")[:10] or "unknown"
        records.append(record)
    table = pa.Table.from_pylist(records)
    pq.write_to_dataset(  # type: ignore[no-untyped-call]
        table, root_path=canonical_root, partition_cols=["source_vendor", "event_date"]
    )
    return canonical_root
