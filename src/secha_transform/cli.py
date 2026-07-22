"""CLI entrypoint: `secha-transform <vendor> --date ...` (raw landing -> canonical)."""

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
from secha_transform.engine.transform import transform_records
from secha_transform.io.delta_sink import DeltaSink
from secha_transform.io.reader import read_device_factors, read_records
from secha_transform.io.writer import write_canonical_parquet
from secha_transform.metadata.loader import MetadataBundle, load_bundle

app = typer.Typer(
    help="SECHA config-driven transform engine (raw -> canonical).", no_args_is_help=True
)


@app.callback()
def _main() -> None:
    """SECHA transform engine (one subcommand per vendor)."""


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


def _merge_and_echo(sink: DeltaSink, staging: str, bundle: MetadataBundle) -> None:
    table = sink.ensure_table(bundle)
    report = sink.merge_staging(staging, bundle)
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
        for stat_field in dataclass_fields(TransformStats):
            name = stat_field.name
            setattr(totals, name, getattr(totals, name) + getattr(result.stats, name))
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
) -> None:
    """MERGE staged canonical parquet into Delta/Unity Catalog (idempotent re-runs)."""
    transform_logging.configure()
    settings = Settings()
    bundle = _load_bundle_or_exit(settings, "mx_electrix")  # any vendor: target layer is shared
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
        _merge_and_echo(delta, staging_path, bundle)
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
