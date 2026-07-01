"""CLI entrypoint: `secha-transform <vendor> --date ...` (raw landing -> canonical)."""

from __future__ import annotations

from typing import Annotated

import typer

from secha_transform import logging as transform_logging
from secha_transform.config import Settings
from secha_transform.engine.transform import transform_records
from secha_transform.io.reader import read_device_factors, read_measurements
from secha_transform.io.writer import write_canonical_parquet
from secha_transform.metadata.loader import load_bundle

app = typer.Typer(
    help="SECHA config-driven transform engine (raw -> canonical).", no_args_is_help=True
)


@app.callback()
def _main() -> None:
    """SECHA transform engine (one subcommand per vendor)."""


@app.command("mx-electrix")
def mx_electrix(
    date: Annotated[str, typer.Option(help="Date to transform, YYYY-MM-DD.")],
    meter: Annotated[str | None, typer.Option(help="Single meter id; omit for all.")] = None,
) -> None:
    """Transform raw MX Electrix `/measurements/` for a date into canonical rows."""
    transform_logging.configure()
    settings = Settings()
    bundle = load_bundle(settings.metadata_root, "mx_electrix")
    records = read_measurements(settings.landing_root, "mx_electrix", date, meter)
    factors = read_device_factors(settings.landing_root, "mx_electrix", date, bundle)
    rows = transform_records(records, bundle, factors)
    path = write_canonical_parquet(rows, settings.canonical_root)
    typer.echo(f"Transformed {len(records)} record(s) -> {len(rows)} canonical rows -> {path}")


if __name__ == "__main__":
    app()
