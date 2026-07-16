"""Env-gated integration test: the sink's SQL against the REAL platform (VPN + .env).

Skipped everywhere the Phase-3 environment is absent (CI, offline dev). With
SECHA_SPARK_URL / SECHA_CATALOG_URL / SECHA_CATALOG_TOKEN set (scripts/phase3/.env
works), it runs one full miniature load into a throwaway table in the real catalog:
create, MERGE twice (idempotency), verify counts, drop. Self-cleaning.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from secha_transform.io.delta_sink import (
    DeltaSink,
    build_create_table_sql,
    build_merge_sql,
    build_staged_projection_sql,
)
from secha_transform.metadata.loader import load_bundle

_REQUIRED = ("SECHA_SPARK_URL", "SECHA_CATALOG_URL", "SECHA_CATALOG_TOKEN")

pytestmark = pytest.mark.skipif(
    any(not os.environ.get(name) for name in _REQUIRED),
    reason="Phase-3 platform env not set (SECHA_SPARK_URL/SECHA_CATALOG_URL/SECHA_CATALOG_TOKEN)",
)


def test_full_miniature_load_and_idempotent_merge(metadata_root: Path) -> None:
    import pyarrow as pa

    bundle = load_bundle(metadata_root, "mx_electrix")
    # throwaway table in the real catalog/schema: never touches canonical.measurement
    test_table = f"_it_{uuid.uuid4().hex[:8]}"
    bundle.target["table"] = test_table
    bundle.canonical["entities"][test_table] = bundle.canonical["entities"]["measurement"]

    rows = [
        {
            "measurement_id": "it-1",
            "source_vendor": "mx_electrix",
            "source_dataset": "it",
            "device_id": "it:device",
            "ts_utc": "2026-01-01T00:00:00Z",
            "quantity": "voltage",
            "phase": "L1",
            "variant": "none",
            "harmonic_order": None,
            "value": 230.0,
            "unit": "V",
            "aggregation": "average",
            "interval_s": 60,
            "quality": "ok",
            "source_row_id": "1",
            "schema_version": "1.0.0",
            "ingested_at": "2026-01-01T01:00:00+00:00",
            "event_date": "2026-01-01",
        },
        {  # same identity, later ingested_at and revised value: dedupe must pick this one
            "measurement_id": "it-1",
            "source_vendor": "mx_electrix",
            "source_dataset": "it",
            "device_id": "it:device",
            "ts_utc": "2026-01-01T00:00:00Z",
            "quantity": "voltage",
            "phase": "L1",
            "variant": "none",
            "harmonic_order": None,
            "value": 231.5,
            "unit": "V",
            "aggregation": "average",
            "interval_s": 60,
            "quality": "ok",
            "source_row_id": "1",
            "schema_version": "1.0.0",
            "ingested_at": "2026-01-01T02:00:00+00:00",
            "event_date": "2026-01-01",
        },
    ]
    # explicit Arrow schema: inference cannot type the all-null harmonic_order column
    arrow_schema = pa.schema(
        [
            ("measurement_id", pa.string()),
            ("source_vendor", pa.string()),
            ("source_dataset", pa.string()),
            ("device_id", pa.string()),
            ("ts_utc", pa.string()),
            ("quantity", pa.string()),
            ("phase", pa.string()),
            ("variant", pa.string()),
            ("harmonic_order", pa.int32()),
            ("value", pa.float64()),
            ("unit", pa.string()),
            ("aggregation", pa.string()),
            ("interval_s", pa.int32()),
            ("quality", pa.string()),
            ("source_row_id", pa.string()),
            ("schema_version", pa.string()),
            ("ingested_at", pa.string()),
            ("event_date", pa.string()),
        ]
    )
    staged_table = pa.Table.from_pylist(rows, schema=arrow_schema)

    sink = DeltaSink(
        spark_url=os.environ["SECHA_SPARK_URL"],
        catalog_url=os.environ["SECHA_CATALOG_URL"],
        token=os.environ["SECHA_CATALOG_TOKEN"],
        catalog=bundle.target["catalog"],
    )
    table = f"{bundle.target['catalog']}.{bundle.target['schema']}.{test_table}"
    try:
        sink._spark.sql(build_create_table_sql(bundle))

        # upload the tiny staged batch through the session (no NFS needed at this size)
        staged = sink._spark.createDataFrame(staged_table)
        staged.createOrReplaceTempView("_secha_staged_raw")
        projection = build_staged_projection_sql(bundle, set(rows[0]))
        sink._spark.sql(projection).createOrReplaceTempView("_secha_staged")

        sink._spark.sql(build_merge_sql(bundle))
        first = sink._spark.sql(f"SELECT count(*), max(value) FROM {table}").collect()[0]
        assert (first[0], first[1]) == (1, 231.5)  # deduped, latest ingested_at won

        sink._spark.sql(build_merge_sql(bundle))  # idempotency: same staged batch again
        second = sink._spark.sql(f"SELECT count(*), max(value) FROM {table}").collect()[0]
        assert (second[0], second[1]) == (1, 231.5)
    finally:
        try:
            sink._spark.sql(f"DROP TABLE IF EXISTS {table}")
        finally:
            sink.stop()
