"""Offline tests for the Delta sink's SQL builders (no pyspark, no cluster).

The builders are pure functions from the metadata bundle to SQL text, so everything
the sink will say to the platform is pinned here: DDL columns/types/properties,
the dedupe + typed projection, the MERGE, and the serving-view wrapper.
"""

from __future__ import annotations

import pytest

from secha_transform.io.delta_sink import (
    build_create_schema_sql,
    build_create_table_sql,
    build_dimension_ddl,
    build_dimension_insert,
    build_merge_sql,
    build_serving_table_ddl,
    build_serving_view_sql,
    build_staged_projection_sql,
    dimension_rows,
    full_table_name,
    serving_mode,
    spark_type_for,
    table_columns,
)
from secha_transform.metadata.loader import MetadataBundle

CANONICAL = {
    "entities": {
        "measurement": {
            "fields": [
                {"name": "measurement_id", "type": "string"},
                {"name": "source_vendor", "type": "enum:source_vendor"},
                {"name": "device_id", "type": "string"},
                {"name": "location_id", "type": "string"},
                {"name": "ts_utc", "type": "timestamp"},
                {"name": "quantity", "type": "vocab:quantity"},
                {"name": "harmonic_order", "type": "int"},
                {"name": "value", "type": "double"},
                {"name": "unit", "type": "registry:unit"},
                {"name": "interval_s", "type": "int"},
                {"name": "ingested_at", "type": "timestamp"},
            ]
        }
    }
}
TARGET = {
    "catalog": "secha",
    "schema": "canonical",
    "table": "measurement",
    "merge_key": ["measurement_id"],
    "partition_by": ["source_vendor", "event_date"],
    "table_properties": {"delta.feature.catalogManaged": "supported"},
    "serving_schema": "serving",
}


def _bundle(target: dict | None = None) -> MetadataBundle:
    return MetadataBundle(
        vendor="demo",
        canonical=CANONICAL,
        vocabulary={},
        units={},
        transforms={},
        target=TARGET if target is None else target,
        source_schema={},
        mapping={},
        validation={},
    )


def test_spark_type_mapping() -> None:
    assert spark_type_for("string") == "STRING"
    assert spark_type_for("timestamp") == "TIMESTAMP"
    assert spark_type_for("long") == "BIGINT"
    assert spark_type_for("enum:phase") == "STRING"
    assert spark_type_for("vocab:quantity") == "STRING"
    assert spark_type_for("registry:unit") == "STRING"


def test_unknown_canonical_type_raises() -> None:
    """A canonical type the sink cannot map must fail loudly, never default silently."""
    with pytest.raises(ValueError, match="no Spark mapping"):
        spark_type_for("geometry")


def test_table_columns_follow_canonical_schema_plus_event_date() -> None:
    columns = dict(table_columns(_bundle()))
    assert columns["value"] == "DOUBLE"
    assert columns["harmonic_order"] == "INT"
    assert columns["ts_utc"] == "TIMESTAMP"
    assert columns["quantity"] == "STRING"
    assert columns["event_date"] == "DATE"  # storage partition column, always appended


def test_create_table_ddl_carries_platform_properties() -> None:
    ddl = build_create_table_sql(_bundle())
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS secha.canonical.measurement")
    assert "USING DELTA" in ddl
    assert "PARTITIONED BY (source_vendor, event_date)" in ddl
    # the rule this platform's handshake taught us, straight from targets config:
    assert "TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported')" in ddl
    assert "value DOUBLE" in ddl and "ts_utc TIMESTAMP" in ddl


def test_create_schema_sql() -> None:
    assert build_create_schema_sql(_bundle()) == "CREATE SCHEMA IF NOT EXISTS secha.canonical"


def test_projection_casts_staged_and_nulls_missing_columns() -> None:
    staged = {
        "measurement_id",
        "source_vendor",
        "device_id",
        "ts_utc",
        "quantity",
        "harmonic_order",
        "value",
        "unit",
        "interval_s",
        "ingested_at",
        "event_date",
    }
    sql = build_staged_projection_sql(_bundle(), staged)
    assert "CAST(ts_utc AS TIMESTAMP) AS ts_utc" in sql
    assert "CAST(value AS DOUBLE) AS value" in sql
    # the engine never emits location_id; the projection supplies a typed NULL
    assert "CAST(NULL AS STRING) AS location_id" in sql
    # in-batch dedupe: latest ingested_at wins on the merge key
    assert "PARTITION BY measurement_id ORDER BY ingested_at DESC" in sql
    assert "WHERE _rn = 1" in sql


def test_merge_sql_uses_configured_key_and_explicit_columns() -> None:
    sql = build_merge_sql(_bundle())
    assert "MERGE INTO secha.canonical.measurement AS t" in sql
    assert "ON t.measurement_id = s.measurement_id" in sql
    assert "WHEN MATCHED THEN UPDATE SET" in sql
    assert "t.value = s.value" in sql
    assert "WHEN NOT MATCHED THEN INSERT" in sql
    assert "s.event_date" in sql  # explicit lists include the partition column


def test_serving_view_sql_wraps_body_and_resolves_placeholder() -> None:
    body = "-- doc\nSELECT device_id FROM {canonical} WHERE quality = 'ok'"
    sql = build_serving_view_sql(_bundle(), "pq_minute_wide", body)
    assert sql.startswith("CREATE OR REPLACE VIEW secha.serving.pq_minute_wide AS")
    assert "FROM secha.canonical.measurement" in sql
    assert "{canonical}" not in sql


def test_serving_table_ddl_uses_analysed_columns_and_platform_properties() -> None:
    """No views, no RTAS on this connector; snapshots use plain DDL + insert-select."""
    columns = [("device_id", "string"), ("minute_utc", "timestamp"), ("voltage_l1_v", "double")]
    sql = build_serving_table_ddl(_bundle(), "pq_minute_wide", columns)
    assert sql.startswith("CREATE TABLE secha.serving.pq_minute_wide")
    assert "device_id string" in sql and "voltage_l1_v double" in sql
    assert "USING DELTA" in sql
    assert "TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported')" in sql
    assert "AS SELECT" not in sql  # never CTAS/RTAS: the connector lacks staged replace


def test_serving_mode_unknown_raises() -> None:
    target = dict(TARGET, serving_mode="materialized")
    with pytest.raises(ValueError, match="serving_mode"):
        serving_mode(_bundle(target))


def test_serving_mode_defaults_to_view() -> None:
    assert serving_mode(_bundle()) == "view"  # TARGET carries no serving_mode
    assert serving_mode(_bundle(dict(TARGET, serving_mode="table"))) == "table"


def test_serving_without_serving_schema_raises() -> None:
    target = {key: value for key, value in TARGET.items() if key != "serving_schema"}
    with pytest.raises(ValueError, match="serving_schema"):
        build_serving_view_sql(_bundle(target), "v", "SELECT 1 FROM {canonical}")


def test_full_table_name_comes_from_target_config() -> None:
    assert full_table_name(_bundle()) == "secha.canonical.measurement"


# --- reference dimension builders (quantity dimension) ----------------------------------

_DIM_COLUMNS = [
    {"name": "quantity", "from": "__key__", "comment": "join key to measurement.quantity"},
    {"name": "default_unit", "from": "default_unit", "comment": "canonical unit"},
    {"name": "standard_ref", "from": "standard_ref"},
    {"name": "description", "from": "description"},
]
_VOCAB = {
    "voltage": {
        "default_unit": "V",
        "standard_ref": "IEC 61000-4-30",
        "description": "RMS voltage",
    },
    "frequency": {"default_unit": "Hz", "standard_ref": "IEC ...", "description": "Freq's value"},
}


def test_dimension_rows_are_deterministic_and_keyed() -> None:
    rows = dimension_rows(_VOCAB, _DIM_COLUMNS)
    # sorted by key: frequency before voltage; __key__ column carries the key
    assert rows[0][0] == "frequency" and rows[1][0] == "voltage"
    assert rows[1] == ["voltage", "V", "IEC 61000-4-30", "RMS voltage"]


def test_dimension_ddl_carries_column_comments_and_properties() -> None:
    ddl = build_dimension_ddl("secha.canonical.quantity", _DIM_COLUMNS, TARGET["table_properties"])
    assert ddl.startswith("CREATE TABLE secha.canonical.quantity")
    assert "quantity STRING COMMENT 'join key to measurement.quantity'" in ddl
    assert "standard_ref STRING," in ddl  # no comment declared -> no COMMENT clause
    assert "USING DELTA" in ddl
    assert "TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported')" in ddl


def test_dimension_insert_escapes_single_quotes() -> None:
    """Descriptions can contain apostrophes; literals must be doubled, never interpolated raw."""
    rows = dimension_rows(_VOCAB, _DIM_COLUMNS)
    sql = build_dimension_insert("secha.canonical.quantity", _DIM_COLUMNS, rows)
    assert sql.startswith("INSERT INTO secha.canonical.quantity (quantity, default_unit,")
    assert "'Freq''s value'" in sql  # apostrophe doubled
    assert "('voltage', 'V', 'IEC 61000-4-30', 'RMS voltage')" in sql


def test_dimension_insert_nulls_missing_attributes() -> None:
    rows = dimension_rows({"x": {"default_unit": "V"}}, _DIM_COLUMNS)
    sql = build_dimension_insert("secha.canonical.quantity", _DIM_COLUMNS, rows)
    assert "('x', 'V', NULL, NULL)" in sql  # absent standard_ref/description -> NULL, not ''
