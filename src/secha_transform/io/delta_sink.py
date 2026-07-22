"""Phase-3 sink: canonical staging parquet -> Delta / Unity Catalog, MERGEd on identity.

Everything is driven by the metadata bundle: the table DDL is generated from
`canonical_schema.yaml` (column names + types) and `targets/canonical.yaml`
(catalog/schema/table, merge key, partitions, platform `table_properties`); serving views
come from the rulebook's `serving/*.sql`. No table name, column, or property is hardcoded.

The sink consumes STAGED parquet (the Phase-1 writer's output, placed on a path the Spark
workers can read, e.g. the cluster NFS). Before MERGE the staging rows are deduplicated on
the merge key, latest `ingested_at` winning, so re-fetched or revised data converges and
re-running the same load is idempotent.

Pure SQL-builder functions live at module top (offline-testable, no pyspark import);
`DeltaSink` wraps the Spark Connect session (pyspark imported lazily: the `spark` extra,
pinned to the server's version, is only needed at run time).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secha_transform.metadata.loader import MetadataBundle

# canonical_schema field types -> Spark SQL types; enum/vocab/registry are string-coded
_SPARK_TYPES = {
    "string": "STRING",
    "timestamp": "TIMESTAMP",
    "int": "INT",
    "long": "BIGINT",
    "double": "DOUBLE",
}
# storage-only partition column, derived from ts_utc by the Phase-1 writer
_EVENT_DATE_COLUMN = ("event_date", "DATE")
_STAGED_RAW_VIEW = "_secha_staged_raw"
_STAGED_VIEW = "_secha_staged"


def spark_type_for(field_type: str) -> str:
    """Map a canonical-schema field type to a Spark SQL type; unknown types raise."""
    if field_type.startswith(("enum:", "vocab:", "registry:")):
        return "STRING"
    if field_type in _SPARK_TYPES:
        return _SPARK_TYPES[field_type]
    raise ValueError(f"canonical field type '{field_type}' has no Spark mapping")


def table_columns(bundle: MetadataBundle) -> list[tuple[str, str]]:
    """(name, spark_type) for every fact column, from the canonical schema + event_date."""
    entity = bundle.target["table"]
    fields = bundle.canonical["entities"][entity]["fields"]
    columns = [(field["name"], spark_type_for(field["type"])) for field in fields]
    columns.append(_EVENT_DATE_COLUMN)
    return columns


def full_table_name(bundle: MetadataBundle) -> str:
    target = bundle.target
    return f"{target['catalog']}.{target['schema']}.{target['table']}"


def build_create_schema_sql(bundle: MetadataBundle) -> str:
    target = bundle.target
    return f"CREATE SCHEMA IF NOT EXISTS {target['catalog']}.{target['schema']}"


def build_create_table_sql(bundle: MetadataBundle) -> str:
    """CREATE TABLE IF NOT EXISTS, columns + partitioning + platform table properties."""
    target = bundle.target
    columns = ",\n  ".join(f"{name} {spark_type}" for name, spark_type in table_columns(bundle))
    partition_cols = ", ".join(target.get("partition_by", []))
    partition_clause = f"\nPARTITIONED BY ({partition_cols})" if partition_cols else ""
    properties = target.get("table_properties") or {}
    property_items = ", ".join(f"'{key}' = '{value}'" for key, value in properties.items())
    properties_clause = f"\nTBLPROPERTIES ({property_items})" if property_items else ""
    return (
        f"CREATE TABLE IF NOT EXISTS {full_table_name(bundle)} (\n  {columns}\n)\n"
        f"USING DELTA{partition_clause}{properties_clause}"
    )


def build_staged_projection_sql(
    bundle: MetadataBundle, available_columns: set[str], raw_view: str = _STAGED_RAW_VIEW
) -> str:
    """Typed, deduplicated projection of the staged parquet.

    Every table column is produced: staged columns are CAST to the canonical type,
    columns the engine does not emit (e.g. location_id) become typed NULLs. Duplicates
    on the merge key are resolved before MERGE (which rejects multiple matches):
    the latest `ingested_at` wins, so revised data converges deterministically.
    """
    casts = ",\n  ".join(
        f"CAST({name} AS {spark_type}) AS {name}"
        if name in available_columns
        else f"CAST(NULL AS {spark_type}) AS {name}"
        for name, spark_type in table_columns(bundle)
    )
    key = bundle.target["merge_key"]
    partition_key = ", ".join(key)
    return (
        f"SELECT\n  {casts}\nFROM (\n"
        f"  SELECT *, row_number() OVER (\n"
        f"    PARTITION BY {partition_key} ORDER BY ingested_at DESC\n"
        f"  ) AS _rn\n"
        f"  FROM {raw_view}\n"
        f") WHERE _rn = 1"
    )


def build_merge_sql(bundle: MetadataBundle, staged_view: str = _STAGED_VIEW) -> str:
    """MERGE on the configured key; explicit column lists (no schema-drift surprises)."""
    columns = [name for name, _ in table_columns(bundle)]
    on = " AND ".join(f"t.{key} = s.{key}" for key in bundle.target["merge_key"])
    update_set = ", ".join(f"t.{name} = s.{name}" for name in columns)
    insert_cols = ", ".join(columns)
    insert_vals = ", ".join(f"s.{name}" for name in columns)
    return (
        f"MERGE INTO {full_table_name(bundle)} AS t\n"
        f"USING {staged_view} AS s\n"
        f"ON {on}\n"
        f"WHEN MATCHED THEN UPDATE SET {update_set}\n"
        f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
    )


# Serving definitions are materialisation-agnostic: serving/*.sql bodies reference the fact
# table only via the {canonical} placeholder (enforced by secha-metadata's validator). The
# target's serving_mode decides the wrapping:
# - `view`: CREATE OR REPLACE VIEW (the natural form).
# - `table`: a Delta snapshot refreshed on each `delta-views` run. This platform's UC Spark
#   connector (0.4.0) has NO view ability (MISSING_CATALOG_ABILITY.VIEWS) and NO staged
#   replace either (RTAS "not supported"), so table mode uses only the primitives the
#   platform has proven: DROP TABLE IF EXISTS + CREATE TABLE (explicit DDL, schema derived
#   by analysing the SELECT) + INSERT INTO ... SELECT. Non-atomic (brief gap during
#   refresh); acceptable for Phase 3, revisit when the connector matures.


def serving_mode(bundle: MetadataBundle) -> str:
    """The target's serving materialisation mode; unknown modes raise."""
    mode = str(bundle.target.get("serving_mode", "view"))
    if mode not in ("view", "table"):
        raise ValueError(f"serving_mode '{mode}' is not implemented by this sink")
    return mode


def serving_table_name(bundle: MetadataBundle, view_name: str) -> str:
    target = bundle.target
    serving_schema = target.get("serving_schema")
    if not serving_schema:
        raise ValueError("targets/canonical.yaml has no serving_schema; cannot create views")
    return f"{target['catalog']}.{serving_schema}.{view_name}"


def resolve_serving_select(bundle: MetadataBundle, body: str) -> str:
    """Resolve the {canonical} placeholder into the fully qualified fact table."""
    return body.replace("{canonical}", full_table_name(bundle)).strip()


def build_serving_view_sql(bundle: MetadataBundle, view_name: str, body: str) -> str:
    qualified = serving_table_name(bundle, view_name)
    return f"CREATE OR REPLACE VIEW {qualified} AS\n{resolve_serving_select(bundle, body)}"


def build_serving_table_ddl(
    bundle: MetadataBundle, view_name: str, columns: list[tuple[str, str]]
) -> str:
    """CREATE TABLE DDL for a serving snapshot, columns as analysed from its SELECT."""
    qualified = serving_table_name(bundle, view_name)
    column_items = ",\n  ".join(f"{name} {sql_type}" for name, sql_type in columns)
    properties = bundle.target.get("table_properties") or {}
    property_items = ", ".join(f"'{key}' = '{value}'" for key, value in properties.items())
    properties_clause = f"\nTBLPROPERTIES ({property_items})" if property_items else ""
    return f"CREATE TABLE {qualified} (\n  {column_items}\n)\nUSING DELTA{properties_clause}"


# Reference dimensions are published directly from the rulebook vocabularies (not from fact
# data), so consumers JOIN the long fact for human-readable semantics + standards. This is the
# correct home for per-quantity meaning in a LONG model and sidesteps the UC 0.4 column-comment
# limitation (the descriptions are row values, always queryable, plus the dimension's own
# columns carry real DDL comments where comments actually mean something).


def _sql_literal(value: Any) -> str:
    """A safe SQL string literal (or NULL); single quotes are doubled, never interpolated raw."""
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def resolve_vocabulary(bundle: MetadataBundle, source_vocabulary: str) -> dict[str, Any]:
    """The rulebook vocabulary a reference dimension is published from; unknown names raise."""
    if source_vocabulary == "quantities":
        return dict(bundle.vocabulary.get("quantities", {}))
    if source_vocabulary == "units":
        return dict(bundle.units.get("units", {}))
    raise ValueError(
        f"reference dimension source_vocabulary '{source_vocabulary}' is not available"
    )


def dimension_rows(vocab: dict[str, Any], columns: list[dict[str, Any]]) -> list[list[Any]]:
    """Ordered rows for a reference dimension; `from: __key__` is the dict key.

    Keys are sorted so the published table is deterministic across runs.
    """
    rows: list[list[Any]] = []
    for key in sorted(vocab):
        entry = vocab[key]
        rows.append(
            [key if col["from"] == "__key__" else entry.get(col["from"]) for col in columns]
        )
    return rows


def build_dimension_ddl(
    qualified: str, columns: list[dict[str, Any]], table_properties: dict[str, Any]
) -> str:
    """CREATE TABLE DDL for a reference dimension (all STRING, with real column comments)."""
    items = []
    for col in columns:
        comment = col.get("comment")
        comment_clause = f" COMMENT {_sql_literal(comment)}" if comment else ""
        items.append(f"{col['name']} STRING{comment_clause}")
    column_items = ",\n  ".join(items)
    property_items = ", ".join(f"'{key}' = '{value}'" for key, value in table_properties.items())
    properties_clause = f"\nTBLPROPERTIES ({property_items})" if property_items else ""
    return f"CREATE TABLE {qualified} (\n  {column_items}\n)\nUSING DELTA{properties_clause}"


def build_dimension_insert(
    qualified: str, columns: list[dict[str, Any]], rows: list[list[Any]]
) -> str:
    """INSERT INTO ... VALUES for a reference dimension; every value a safe SQL literal."""
    column_names = ", ".join(col["name"] for col in columns)
    value_tuples = ",\n  ".join(
        "(" + ", ".join(_sql_literal(value) for value in row) + ")" for row in rows
    )
    return f"INSERT INTO {qualified} ({column_names}) VALUES\n  {value_tuples}"


@dataclass(frozen=True)
class MergeReport:
    """Counted outcome of one staged load; every number is user-visible."""

    staged_rows: int
    merged_rows: int  # after in-batch dedupe on the merge key
    table_rows_before: int
    table_rows_after: int


class DeltaSink:
    """Spark Connect session + the Delta/UC operations, config-driven end to end."""

    def __init__(self, spark_url: str, catalog_url: str, token: str, catalog: str) -> None:
        try:
            from pyspark.sql import SparkSession
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "pyspark-client is not installed; install the 'spark' extra "
                "(pip install -e '.[spark]')"
            ) from exc
        self._spark = (
            SparkSession.builder.config(
                f"spark.sql.catalog.{catalog}", "io.unitycatalog.spark.UCSingleCatalog"
            )
            .config(f"spark.sql.catalog.{catalog}.uri", catalog_url)
            .config(f"spark.sql.catalog.{catalog}.token", token)
            .config("spark.sql.defaultCatalog", catalog)
            .remote(spark_url)
            .getOrCreate()
        )

    def ensure_table(self, bundle: MetadataBundle) -> str:
        """Create schema + fact table if absent (DDL generated from the rulebook)."""
        self._spark.sql(build_create_schema_sql(bundle))
        self._spark.sql(build_create_table_sql(bundle))
        return full_table_name(bundle)

    def merge_staging(self, staging_path: str, bundle: MetadataBundle) -> MergeReport:
        """Read staged parquet (cluster-visible path), dedupe, MERGE; return counts."""
        table = full_table_name(bundle)
        path = staging_path if "://" in staging_path else f"file://{staging_path}"
        staged = self._spark.read.option("basePath", path).parquet(path)
        staged.createOrReplaceTempView(_STAGED_RAW_VIEW)
        staged_rows = staged.count()

        projection = build_staged_projection_sql(bundle, set(staged.columns))
        self._spark.sql(projection).createOrReplaceTempView(_STAGED_VIEW)
        merged_rows = self._spark.sql(f"SELECT count(*) FROM {_STAGED_VIEW}").collect()[0][0]

        before = self._spark.sql(f"SELECT count(*) FROM {table}").collect()[0][0]
        self._spark.sql(build_merge_sql(bundle))
        after = self._spark.sql(f"SELECT count(*) FROM {table}").collect()[0][0]
        return MergeReport(
            staged_rows=staged_rows,
            merged_rows=merged_rows,
            table_rows_before=before,
            table_rows_after=after,
        )

    def create_reference_dimensions(self, bundle: MetadataBundle) -> list[str]:
        """Publish every rulebook reference dimension as a Delta table; returns qualified names.

        Uses only platform-proven primitives (drop, create with explicit DDL, insert-values),
        so it works on the UC 0.4 connector that lacks views and RTAS.
        """
        target = bundle.target
        catalog = target["catalog"]
        properties = target.get("table_properties") or {}
        created: list[str] = []
        for name, decl in sorted((target.get("reference_dimensions") or {}).items()):
            schema = decl.get("schema", target["schema"])
            qualified = f"{catalog}.{schema}.{name}"
            columns = decl["columns"]
            rows = dimension_rows(resolve_vocabulary(bundle, decl["source_vocabulary"]), columns)
            self._spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")
            self._spark.sql(f"DROP TABLE IF EXISTS {qualified}")
            self._spark.sql(build_dimension_ddl(qualified, columns, properties))
            if rows:
                self._spark.sql(build_dimension_insert(qualified, columns, rows))
            created.append(qualified)
        return created

    def create_serving_views(self, bundle: MetadataBundle, serving_dir: Path) -> list[str]:
        """Create/refresh every rulebook serving definition; returns the qualified names.

        Materialised per the target's serving_mode: `view`, or `table` (a Delta snapshot
        built with the platform-proven primitives only: drop, create with explicit DDL
        from the analysed SELECT schema, insert-select).
        """
        target = bundle.target
        mode = serving_mode(bundle)
        serving_schema = target.get("serving_schema")
        if not serving_schema:
            raise ValueError("targets/canonical.yaml has no serving_schema; cannot create views")
        self._spark.sql(f"CREATE SCHEMA IF NOT EXISTS {target['catalog']}.{serving_schema}")
        created: list[str] = []
        for path in sorted(serving_dir.glob("*.sql")):
            body = path.read_text(encoding="utf-8")
            qualified = serving_table_name(bundle, path.stem)
            if mode == "view":
                self._spark.sql(build_serving_view_sql(bundle, path.stem, body))
            else:
                select_body = resolve_serving_select(bundle, body)
                # analyse (not execute) the SELECT to learn the snapshot's schema
                schema = self._spark.sql(select_body).schema
                columns = [(field.name, field.dataType.simpleString()) for field in schema]
                self._spark.sql(f"DROP TABLE IF EXISTS {qualified}")
                self._spark.sql(build_serving_table_ddl(bundle, path.stem, columns))
                self._spark.sql(f"INSERT INTO {qualified}\n{select_body}")
            created.append(qualified)
        return created

    def query(self, sql: str) -> list[Any]:
        """Small escape hatch for verification queries (used by the CLI + Step 4)."""
        rows: list[Any] = self._spark.sql(sql).collect()
        return rows

    def stop(self) -> None:
        self._spark.stop()
