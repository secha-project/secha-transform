"""CLI entrypoint: `secha-transform run <vendor>` (raw landing -> canonical).

`run` serves any vendor straight from its metadata. The first two vendors keep their own
commands: MX Electrix scales values with factors from a second source (its device list),
and both predate `run`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import fields as dataclass_fields
from datetime import date as date_type
from pathlib import Path
from typing import Annotated, Any

import typer

from secha_transform import logging as transform_logging
from secha_transform.config import Settings
from secha_transform.engine.models import TransformStats
from secha_transform.engine.transform import entity_fields, transform_records
from secha_transform.io.delta_sink import DeltaSink, entity_target
from secha_transform.io.reader import (
    iter_partitions,
    layout_keys,
    read_device_factors,
    read_partition,
    read_records,
)
from secha_transform.io.writer import write_canonical_parquet, write_entity_parquet
from secha_transform.metadata.loader import MetadataBundle, load_bundle

app = typer.Typer(
    help="SECHA config-driven transform engine (raw -> canonical).", no_args_is_help=True
)


@app.callback()
def _main() -> None:
    """SECHA transform engine: `run <vendor>` for any vendor, plus the first two vendors' own
    commands and the Delta / Unity Catalog commands."""


def _validate_date(date: str) -> None:
    try:
        date_type.fromisoformat(date)
    except ValueError as exc:
        raise typer.BadParameter(f"--date must be YYYY-MM-DD, got {date!r}") from exc


def _load_bundle_or_exit(settings: Settings, vendor: str) -> MetadataBundle:
    try:
        return load_bundle(settings.metadata_root, vendor)
    except FileNotFoundError as exc:
        typer.secho(
            f"{exc}. Point SECHA_METADATA_ROOT at a secha-metadata checkout (see .env.template)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc


def _echo_summary(stats: TransformStats, path: str) -> None:
    typer.echo(
        f"Transformed {stats.records_in} record(s) -> {stats.rows_emitted} canonical rows "
        f"({stats.rows_suspect} suspect, {stats.rows_dropped} dropped, "
        f"{stats.records_rejected} rejected, {stats.records_unmapped} unmapped) -> {path}"
    )


def _delta_sink_or_exit(settings: Settings, bundle: MetadataBundle) -> DeltaSink:
    missing = [
        name
        for name, value in (
            ("SECHA_SPARK_URL", settings.spark_url),
            ("SECHA_CATALOG_URL", settings.catalog_url),
            ("SECHA_CATALOG_TOKEN", settings.catalog_token),
        )
        if not value
    ]
    if missing:
        typer.secho(
            f"Delta sink needs {', '.join(missing)} in .env (see .env.template; TUNI VPN required)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        return DeltaSink(
            spark_url=settings.spark_url,
            catalog_url=settings.catalog_url,
            token=settings.catalog_token,
            catalog=bundle.target["catalog"],
        )
    except RuntimeError as exc:  # missing optional extra
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def _merge_and_echo(
    sink: DeltaSink, staging: str, bundle: MetadataBundle, entity: str | None = None
) -> None:
    table = sink.ensure_table(bundle, entity)
    report = sink.merge_staging(staging, bundle, entity)
    typer.echo(
        f"MERGE into {table}: staged {report.staged_rows} rows "
        f"({report.merged_rows} after dedupe on the merge key); "
        f"table {report.table_rows_before} -> {report.table_rows_after} rows"
    )


_SINK_HELP = (
    "Where canonical rows go: 'parquet' (local dataset, default) or 'delta' (additionally "
    "MERGE the written dataset into Delta/Unity Catalog; requires the written path to be "
    "cluster-visible, e.g. SECHA_CANONICAL_ROOT on the NFS)."
)


def _validate_sink(sink: str) -> None:
    if sink not in ("parquet", "delta"):
        raise typer.BadParameter(f"--sink must be 'parquet' or 'delta', got {sink!r}")


@app.command("mx-electrix")
def mx_electrix(
    date: Annotated[str, typer.Option(help="Date to transform, YYYY-MM-DD.")],
    meter: Annotated[str | None, typer.Option(help="Single meter id; omit for all.")] = None,
    sink: Annotated[str, typer.Option(help=_SINK_HELP)] = "parquet",
) -> None:
    """Transform raw MX Electrix `/measurements/` for a date into canonical rows."""
    transform_logging.configure()
    _validate_date(date)
    _validate_sink(sink)
    settings = Settings()
    bundle = _load_bundle_or_exit(settings, "mx_electrix")

    records = read_records(settings.landing_root, bundle.source_schema, date, meter)
    factors = read_device_factors(settings.landing_root, "mx_electrix", date, bundle)
    result = transform_records(records, bundle, factors)
    run_tag = f"{date}-meter-{meter or 'all'}"
    path = write_canonical_parquet(result.rows, settings.canonical_root, run_tag=run_tag)
    _echo_summary(result.stats, path)
    if sink == "delta":
        delta = _delta_sink_or_exit(settings, bundle)
        try:
            _merge_and_echo(delta, settings.canonical_root, bundle)
        finally:
            delta.stop()


def _batched(records: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for record in records:
        batch.append(record)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _add_stats(totals: TransformStats, stats: TransformStats) -> None:
    for stat_field in dataclass_fields(TransformStats):
        name = stat_field.name
        setattr(totals, name, getattr(totals, name) + getattr(stats, name))


def _parse_selection(select: list[str], layout: str) -> dict[str, str]:
    keys = layout_keys(layout)
    selection: dict[str, str] = {}
    for item in select:
        key, separator, value = item.partition("=")
        if not (separator and key and value):
            raise typer.BadParameter(f"--select takes key=value, got {item!r}")
        if key not in keys:
            raise typer.BadParameter(f"--select {key!r}: the layout's keys are {keys}")
        selection[key] = value
    return selection


@app.command("run")
def run_vendor(
    vendor: Annotated[str, typer.Argument(help="Vendor directory in secha-metadata.")],
    select: Annotated[
        list[str] | None,
        typer.Option(
            "--select",
            help="Partitions to transform, as a layout key=value (repeatable). A layout key "
            "left out takes every landed value.",
        ),
    ] = None,
    batch_size: Annotated[
        int, typer.Option(help="Records per transform/write batch (memory ceiling).")
    ] = 100_000,
) -> None:
    """Transform one vendor's landed partitions into canonical rows, from its metadata alone.

    Each partition streams in batches, and each batch writes part files named after its
    partition, so re-running a partition replaces its own output (idempotent). A source
    that declares sessions also writes one charging_session row per session, to
    SECHA_DIMENSIONS_ROOT/charging_session.
    """
    transform_logging.configure()
    settings = Settings()
    bundle = _load_bundle_or_exit(settings, vendor)
    if bundle.source_schema.get("device_factors"):
        typer.secho(
            f"{vendor} scales values with device factors from a second source, which `run` "
            "does not read; use its own command",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    selection = _parse_selection(select or [], bundle.source_schema["access"]["layout"])
    partitions = list(iter_partitions(settings.landing_root, bundle.source_schema, **selection))
    if not partitions:
        typer.secho(
            f"No landed partitions for {vendor} match {selection or 'the layout'} under "
            f"{settings.landing_root}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    sessions_root = f"{settings.dimensions_root}/charging_session"
    session_types = entity_fields(bundle, "charging_session")
    totals = TransformStats()
    sessions_seen: set[str] = set()
    for number, partition in enumerate(partitions, start=1):
        records = read_partition(settings.landing_root, partition, bundle.source_schema)
        partition_stats = TransformStats()
        # A session can straddle two batches; each partition writes it once. Tracking this
        # per partition, not per run, keeps a re-run of one partition byte-identical. A
        # session that straddles two partitions is written by both and merged on its key.
        partition_sessions: set[str] = set()
        for index, batch in enumerate(_batched(records, batch_size)):
            result = transform_records(batch, bundle)
            run_tag = f"{partition.identity or 'all'}-b{index:05d}"
            write_canonical_parquet(result.rows, settings.canonical_root, run_tag=run_tag)
            new_sessions = [
                session
                for session in result.sessions
                if str(session["session_id"]) not in partition_sessions
            ]
            partition_sessions.update(str(session["session_id"]) for session in new_sessions)
            write_entity_parquet(new_sessions, sessions_root, session_types, run_tag=run_tag)
            _add_stats(partition_stats, result.stats)
        sessions_seen.update(partition_sessions)
        _add_stats(totals, partition_stats)
        typer.echo(
            f"[{number}/{len(partitions)}] {partition.identity}: "
            f"{partition_stats.records_in:,} records -> {partition_stats.rows_emitted:,} rows"
        )
    _echo_summary(totals, settings.canonical_root)
    if sessions_seen:
        typer.echo(f"{len(sessions_seen):,} distinct charging session(s) -> {sessions_root}")


@app.command("procem")
def procem(
    date: Annotated[str, typer.Option(help="Landing date (ProCem LOCAL day), YYYY-MM-DD.")],
    batch_size: Annotated[
        int, typer.Option(help="Records per transform/write batch (memory ceiling).")
    ] = 500_000,
    sink: Annotated[str, typer.Option(help=_SINK_HELP)] = "parquet",
) -> None:
    """Transform raw ProCem daily-dump triples for a date into canonical rows.

    ProCem days are tens of millions of records, so the pipeline streams: records are
    read lazily and processed in batches; each batch writes run-scoped part files, so
    re-running the same date replaces the same output (idempotent).
    """
    transform_logging.configure()
    _validate_date(date)
    _validate_sink(sink)
    settings = Settings()
    bundle = _load_bundle_or_exit(settings, "procem_kampusareena_pq")

    records = read_records(settings.landing_root, bundle.source_schema, date)
    totals = TransformStats()
    path = settings.canonical_root
    for index, batch in enumerate(_batched(records, batch_size)):
        result = transform_records(batch, bundle)
        path = write_canonical_parquet(
            result.rows, settings.canonical_root, run_tag=f"{date}-batch{index:05d}"
        )
        _add_stats(totals, result.stats)
    _echo_summary(totals, path)
    if sink == "delta":
        delta = _delta_sink_or_exit(settings, bundle)
        try:
            _merge_and_echo(delta, settings.canonical_root, bundle)
        finally:
            delta.stop()


@app.command("delta-load")
def delta_load(
    staging: Annotated[
        str | None,
        typer.Option(
            help="CLUSTER-VISIBLE staging path holding canonical parquet (e.g. under "
            "/net/nfs/data/secha/canonical-staging); defaults to SECHA_STAGING_ROOT."
        ),
    ] = None,
    entity: Annotated[
        str | None,
        typer.Option(
            help="Load a dimension declared in the target binding (e.g. charging_session) "
            "instead of the measurement table."
        ),
    ] = None,
) -> None:
    """MERGE staged canonical parquet into Delta/Unity Catalog (idempotent re-runs)."""
    transform_logging.configure()
    settings = Settings()
    bundle = _load_bundle_or_exit(settings, "mx_electrix")  # any vendor: target layer is shared
    try:
        entity_target(bundle, entity)  # an undeclared entity fails here, before connecting
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    staging_path = staging or settings.staging_root
    if not staging_path:
        typer.secho(
            "No staging path: pass --staging or set SECHA_STAGING_ROOT (see .env.template)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    delta = _delta_sink_or_exit(settings, bundle)
    try:
        _merge_and_echo(delta, staging_path, bundle, entity)
    finally:
        delta.stop()


@app.command("delta-views")
def delta_views() -> None:
    """Publish the rulebook's derived catalog objects: reference dimensions + serving views."""
    transform_logging.configure()
    settings = Settings()
    bundle = _load_bundle_or_exit(settings, "mx_electrix")  # any vendor: target layer is shared
    serving_dir = Path(settings.metadata_root) / "serving"
    if not serving_dir.is_dir():
        typer.secho(
            f"No serving directory at {serving_dir} (is SECHA_METADATA_ROOT current?)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    delta = _delta_sink_or_exit(settings, bundle)
    try:
        # dimensions first: serving definitions may join them
        dimensions = delta.create_reference_dimensions(bundle)
        views = delta.create_serving_views(bundle, serving_dir)
    finally:
        delta.stop()
    for name in dimensions:
        typer.echo(f"dimension ready: {name}")
    for name in views:
        typer.echo(f"view ready: {name}")


if __name__ == "__main__":
    app()
