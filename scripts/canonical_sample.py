"""Export a small, real sample of canonical output as a workbook a reader can inspect.

Explaining a long fact table in prose rarely lands. This runs the engine over a minute
of landed data per vendor and writes what came out: the canonical rows themselves, the
same rows through the serving view, the run statistics, and the dictionary that says
what every field and every allowed value means.

Nothing here is written by hand. The rows come from `transform_records`, the same call
the CLI makes, and the wide sheet is produced by executing `serving/pq_minute_wide.sql`
from the rulebook. That file targets Spark; SQLite runs it here with one substitution,
recorded in the workbook, because SQLite has no `date_trunc`.

The sample contains real readings. It is project-internal, and the workbook says so on
its first sheet.

`openpyxl` is imported lazily: it is a reporting dependency, not an engine one.

Usage:
    python scripts/canonical_sample.py --out ../review
    python scripts/canonical_sample.py --mx-minutes 5 --procem-seconds 60
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from secha_transform.config import Settings
from secha_transform.engine.models import CanonicalRow
from secha_transform.engine.transform import transform_records
from secha_transform.io.reader import read_device_factors, read_records
from secha_transform.metadata.loader import load_bundle

REPO = Path(__file__).resolve().parents[1]

# The order a reader wants: what the reading is, then the reading, then the bookkeeping.
ROW_FIELDS = [
    "source_vendor",
    "source_dataset",
    "device_id",
    "ts_utc",
    "quantity",
    "phase",
    "variant",
    "harmonic_order",
    "value",
    "unit",
    "aggregation",
    "interval_s",
    "quality",
    "source_row_id",
    "schema_version",
    "measurement_id",
    "ingested_at",
    "location_id",
    "session_id",
    "ts_session_offset_s",
]
STATS_FIELDS = [
    ("records_in", "records read from the landing zone"),
    ("records_rejected", "records rejected by a record rule, nothing emitted for them"),
    ("records_unmapped", "records whose key is not in the mapping, counted not dropped"),
    ("rows_emitted", "canonical rows written"),
    ("rows_suspect", "rows kept but marked suspect by a range rule"),
    ("rows_dropped", "rows discarded by a rule"),
    ("cells_null_skipped", "empty source cells, which produce no row"),
]
MINUTE_IN_SPARK = "date_trunc('minute', CAST(ts_utc AS TIMESTAMP))"
MINUTE_IN_SQLITE = "substr(ts_utc, 1, 16) || ':00Z'"


def _git_commit(path: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _only_date(landing_root: Path, vendor: str, source: str) -> str:
    dates = sorted(
        p.name.split("=", 1)[1]
        for p in (landing_root / f"vendor={vendor}" / f"source={source}").glob("date=*")
    )
    if not dates:
        raise SystemExit(f"no landed dates for {vendor}/{source} under {landing_root}")
    return dates[0]


def _epoch_ms(record: dict[str, Any], field: str) -> int | None:
    try:
        return int(str(record.get(field)).strip())
    except (TypeError, ValueError):
        return None


def sample_mx(settings: Settings, date_value: str, meter: str, minutes: int) -> dict[str, Any]:
    """The first `minutes` records of one meter: this source reports once a minute."""
    bundle = load_bundle(settings.metadata_root, "mx_electrix")
    records = list(read_records(settings.landing_root, bundle.source_schema, date_value, meter))[
        :minutes
    ]
    factors = read_device_factors(settings.landing_root, "mx_electrix", date_value, bundle)
    result = transform_records(records, bundle, factors)
    return {
        "vendor": "mx_electrix",
        "date": date_value,
        "scope": f"meter {meter}, the first {len(records)} records of the day",
        "records": len(records),
        "rows": result.rows,
        "stats": result.stats,
        "mapping_version": bundle.mapping.get("mapping_version", ""),
    }


def sample_procem(settings: Settings, date_value: str, seconds: int) -> dict[str, Any]:
    """One window of the daily dump: this source reports every point every second."""
    bundle = load_bundle(settings.metadata_root, "procem_kampusareena_pq")
    stamp_field = bundle.source_schema["record"]["timestamp_field"]
    records: list[dict[str, Any]] = []
    start: int | None = None
    for record in read_records(settings.landing_root, bundle.source_schema, date_value):
        moment = _epoch_ms(record, stamp_field)
        if moment is None:
            continue
        start = moment if start is None else start
        if moment >= start + seconds * 1000:
            break
        records.append(record)
    result = transform_records(records, bundle)
    return {
        "vendor": "procem_kampusareena_pq",
        "date": date_value,
        "scope": f"the first {seconds} seconds of the day, every point",
        "records": len(records),
        "rows": result.rows,
        "stats": result.stats,
        "mapping_version": bundle.mapping.get("mapping_version", ""),
    }


def serving_view(
    rows: list[CanonicalRow], sql_path: Path
) -> tuple[list[str], list[list[Any]], str]:
    """Run the rulebook's serving SELECT over these rows, in SQLite."""
    sql = sql_path.read_text(encoding="utf-8")
    if sql.count(MINUTE_IN_SPARK) != 2:
        raise SystemExit(f"{sql_path.name} no longer groups by {MINUTE_IN_SPARK}")
    runnable = sql.replace("{canonical}", "canonical").replace(MINUTE_IN_SPARK, MINUTE_IN_SQLITE)

    connection = sqlite3.connect(":memory:")
    columns = ", ".join(f"{name} TEXT" if name != "value" else "value REAL" for name in ROW_FIELDS)
    connection.execute(f"CREATE TABLE canonical ({columns})")
    connection.executemany(
        f"INSERT INTO canonical VALUES ({', '.join('?' * len(ROW_FIELDS))})",
        [[row.to_dict().get(name) for name in ROW_FIELDS] for row in rows],
    )
    cursor = connection.execute(runnable)
    headers = [description[0] for description in cursor.description]
    data = [list(record) for record in cursor.fetchall()]
    connection.close()
    return headers, data, sql


# --------------------------------------------------------------------------- workbook


def _header(sheet: Any, headers: list[str], widths: dict[str, int] | None = None) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    fill = PatternFill("solid", fgColor="EDEDED")
    border = Border(bottom=Side(style="thin", color="999999"))
    for index, name in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=index, value=name)
        cell.font = Font(bold=True)
        cell.fill = fill
        cell.border = border
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        width = (widths or {}).get(name, max(11, min(len(name) + 4, 28)))
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"


def _guide(sheet: Any, samples: list[dict[str, Any]], metadata_root: Path) -> None:
    from openpyxl.styles import Alignment, Font

    sheet.column_dimensions["A"].width = 30
    sheet.column_dimensions["B"].width = 96
    lines: list[tuple[str, str]] = [
        ("Canonical data sample", ""),
        ("", ""),
        (
            "What this is",
            "A minute of real data from each source, put through the engine. The long sheet"
            " is what the canonical table holds. The wide sheet is the same readings through"
            " the serving view that analysis would query.",
        ),
        (
            "Please treat as internal",
            "These are real readings from partner meters, shared inside the project for"
            " review. Please do not redistribute them.",
        ),
        (
            "How the wide sheet was made",
            f"By running serving/pq_minute_wide.sql over the rows in the long sheet. That file"
            f" targets Spark, so {MINUTE_IN_SPARK} was replaced by {MINUTE_IN_SQLITE} to run it"
            " in SQLite. Nothing else was changed; the SQL is on its own sheet.",
        ),
        (
            "Why the dates look odd",
            "A landing partition is a local day, while ts_utc is UTC, so a Finnish day begins"
            " at 21:00 or 22:00 UTC on the day before. Each sample below states the first"
            " timestamp it actually contains.",
        ),
        ("", ""),
    ]
    for sample in samples:
        stats = sample["stats"]
        lines += [
            (sample["vendor"], f"{sample['date']}, {sample['scope']}"),
            ("   mapping version", str(sample["mapping_version"])),
            ("   first timestamp", str(sample["rows"][0].ts_utc) if sample["rows"] else "no rows"),
        ]
        lines += [
            (f"   {name.replace('_', ' ')}", f"{getattr(stats, name)}  ({meaning})")
            for name, meaning in STATS_FIELDS
        ]
        lines.append(("", ""))
    lines += [
        ("Engine commit", _git_commit(REPO)),
        ("Rulebook commit", _git_commit(metadata_root)),
        ("Generated", date.today().isoformat()),
    ]
    for index, (label, value) in enumerate(lines, start=1):
        left = sheet.cell(row=index, column=1, value=label)
        left.font = Font(bold=True)
        left.alignment = Alignment(vertical="top")
        right = sheet.cell(row=index, column=2, value=value)
        right.alignment = Alignment(vertical="top", wrap_text=True)
    sheet.cell(row=1, column=1).font = Font(bold=True, size=14)


def _dictionary_sheets(book: Any, metadata_root: Path) -> None:
    from openpyxl.styles import Alignment

    canonical = yaml.safe_load(
        (metadata_root / "canonical" / "canonical_schema.yaml").read_text(encoding="utf-8")
    )
    vocab = yaml.safe_load(
        (metadata_root / "canonical" / "quantity_vocabulary.yaml").read_text(encoding="utf-8")
    )

    sheet = book.create_sheet("Row fields")
    _header(
        sheet,
        ["Field", "Type", "Can be empty", "What it means"],
        {"Field": 22, "Type": 20, "Can be empty": 14, "What it means": 78},
    )
    for field in canonical["entities"]["measurement"]["fields"]:
        sheet.append(
            [
                field["name"],
                str(field.get("type", "")),
                "yes" if field.get("nullable") else "no",
                field.get("desc", ""),
            ]
        )
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    sheet = book.create_sheet("Quantities")
    _header(
        sheet,
        ["Canonical quantity", "Default unit", "What it means", "Standard reference"],
        {"Canonical quantity": 30, "What it means": 56, "Standard reference": 40},
    )
    for name, meta in vocab["quantities"].items():
        sheet.append(
            [
                name,
                meta.get("default_unit", ""),
                meta.get("description", ""),
                meta.get("standard_ref", ""),
            ]
        )
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    sheet = book.create_sheet("Allowed values")
    _header(sheet, ["Field", "Allowed values"], {"Field": 22, "Allowed values": 96})
    for name, values in canonical["enums"].items():
        sheet.append([name, ", ".join(str(value) for value in values)])
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def build_workbook(samples: list[dict[str, Any]], metadata_root: Path, out_dir: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    rows = [row for sample in samples for row in sample["rows"]]
    headers, wide, sql = serving_view(rows, metadata_root / "serving" / "pq_minute_wide.sql")

    book = Workbook()
    _guide(book.active, samples, metadata_root)
    book.active.title = "How to read this"

    sheet = book.create_sheet("Canonical rows (long)")
    widths = {"device_id": 28, "ts_utc": 26, "measurement_id": 34, "quantity": 26}
    _header(sheet, ROW_FIELDS, widths)
    for row in rows:
        values = row.to_dict()
        sheet.append([values.get(name) for name in ROW_FIELDS])

    sheet = book.create_sheet("Serving view (wide)")
    _header(sheet, headers, {"minute_utc": 24, "device_id": 28, "source_vendor": 22})
    for record in wide:
        sheet.append(record)

    sheet = book.create_sheet("Serving view SQL")
    sheet.column_dimensions["A"].width = 110
    sheet.cell(row=1, column=1, value="serving/pq_minute_wide.sql, as it sits in the rulebook")
    sheet.cell(row=1, column=1).font = Font(bold=True)
    for index, line in enumerate(sql.splitlines(), start=3):
        cell = sheet.cell(row=index, column=1, value=line)
        cell.font = Font(name="Consolas", size=9)
        cell.alignment = Alignment(vertical="top")

    _dictionary_sheets(book, metadata_root)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"SECHA_canonical_sample_{date.today().isoformat()}.xlsx"
    book.save(path)
    return path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--mx-date", help="default: the one date landed for this source")
    parser.add_argument("--mx-meter", default="21")
    parser.add_argument("--mx-minutes", type=int, default=5, help="records, one per minute")
    parser.add_argument("--procem-date", help="default: the one date landed for this source")
    parser.add_argument("--procem-seconds", type=int, default=60)
    parser.add_argument("--out", type=Path, default=REPO / "review")
    args = parser.parse_args(argv)

    settings = Settings()
    landing = Path(settings.landing_root)
    metadata_root = Path(settings.metadata_root).resolve()
    mx_date = args.mx_date or _only_date(landing, "mx_electrix", "measurements")
    procem_date = args.procem_date or _only_date(landing, "procem_kampusareena_pq", "daily_dump")

    samples = [
        sample_mx(settings, mx_date, args.mx_meter, args.mx_minutes),
        sample_procem(settings, procem_date, args.procem_seconds),
    ]
    for sample in samples:
        print(
            f"{sample['vendor']}: {sample['records']} records -> "
            f"{len(sample['rows'])} canonical rows ({sample['scope']})"
        )
    print(f"written: {build_workbook(samples, metadata_root, args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
