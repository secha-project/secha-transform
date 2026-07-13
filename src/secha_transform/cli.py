"""CLI entrypoint: `secha-transform <vendor> --date ...` (raw landing -> canonical)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import fields as dataclass_fields
from datetime import date as date_type
from typing import Annotated, Any

import typer

from secha_transform import logging as transform_logging
from secha_transform.config import Settings
from secha_transform.engine.models import TransformStats
from secha_transform.engine.transform import transform_records
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


@app.command("mx-electrix")
def mx_electrix(
    date: Annotated[str, typer.Option(help="Date to transform, YYYY-MM-DD.")],
    meter: Annotated[str | None, typer.Option(help="Single meter id; omit for all.")] = None,
) -> None:
    """Transform raw MX Electrix `/measurements/` for a date into canonical rows."""
    transform_logging.configure()
    _validate_date(date)
    settings = Settings()
    bundle = _load_bundle_or_exit(settings, "mx_electrix")

    records = read_records(settings.landing_root, bundle.source_schema, date, meter)
    factors = read_device_factors(settings.landing_root, "mx_electrix", date, bundle)
    result = transform_records(records, bundle, factors)
    run_tag = f"{date}-meter-{meter or 'all'}"
    path = write_canonical_parquet(result.rows, settings.canonical_root, run_tag=run_tag)
    _echo_summary(result.stats, path)


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
) -> None:
    """Transform raw ProCem daily-dump triples for a date into canonical rows.

    ProCem days are tens of millions of records, so the pipeline streams: records are
    read lazily and processed in batches; each batch writes run-scoped part files, so
    re-running the same date replaces the same output (idempotent).
    """
    transform_logging.configure()
    _validate_date(date)
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


if __name__ == "__main__":
    app()
