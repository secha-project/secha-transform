# secha-transform

> The **deterministic, config-driven transform engine** for SECHA: raw vendor data → canonical.

`secha-transform` reads **raw** data (from the `secha-ingestion` landing zone) plus the **rulebook**
(`secha-metadata`) and produces **canonical** rows. It is a *deterministic interpreter of metadata*:
**no vendor logic lives in the engine**. Swap a config, get different output, zero code change. That
decoupling is the central interoperability claim of the thesis.

## Architecture at a glance

![secha-transform data flow](docs/secha-transform-data-flow.svg)

Two inputs meet at the engine: **raw JSON** (data) from `secha-ingestion` and the **rulebook** (rules)
from `secha-metadata`. The engine reads the raw records plus per-device factors, **unpivots** the wide
record into long rows while applying transforms (scale by `uk`/`ik`, attach UTC), assigns a stable
`measurement_id`, and writes canonical rows: **parquet locally now (Phase 1)**, Delta / Unity Catalog
with wide serving views later (Phase 3).

## Where this fits in the SECHA system
```
secha-ingestion  →  raw JSON (Bronze)
secha-metadata   →  the transformation rulebook (config-as-code)
secha-transform  →  reads raw + rulebook → canonical (Delta / Unity Catalog)   ← this repo
```

## What it does (the wide → long unpivot)
One wide source record (e.g. ~240 MX Electrix columns) is **unpivoted into many long canonical rows**,
one per measured quantity. Each row is self-describing: `quantity · phase · variant · harmonic_order`
+ `value` + `unit`, tagged with the source field it came from and a deterministic `measurement_id`.
The same physical thing always takes the same row shape across vendors, which is what makes the data
interoperable.

**On the canonical shape (long vs wide).** Only `quantity` (with `value` and `unit`) carries meaning for
most variables. `phase` and `variant` sit at `none` and `harmonic_order` is `null` unless the row is a
phase-resolved or harmonic power-quality reading, so non-PQ data (a battery state-of-charge, a price)
only sets `quantity`. The long form is the **interoperability substrate**, not what consumers query:
Phase 3 builds **wide serving views** (one column per quantity) shaped per use case, so analysts get a
friendly wide table while the long form does the flexible plumbing underneath.

## Proven end-to-end (Phase 1)
A full day of real MX Electrix data (**1,440 one-minute records** for a meter) transforms into
**~36,000 canonical long rows** — each record fans out into one row per measured quantity. The golden
test asserts the engine reproduces the exact canonical rows defined by the `secha-metadata` contract.

## Principles
- **Deterministic & pure.** `transform_records(records, bundle, factors) → rows` is a pure function;
  same input + same config gives identical output (apart from the `ingested_at` stamp).
- **Vendor-blind.** The engine never contains a vendor name or `if vendor == …`; all vendor knowledge
  comes from the `secha-metadata` bundle it loads.
- **Idempotent.** `measurement_id` is a hash of the identity tuple, so re-runs MERGE safely.
- **Config is the contract.** The engine must satisfy the golden fixture defined in `secha-metadata`.

## Layout
```
src/secha_transform/
  metadata/   loader.py — load the rulebook into a MetadataBundle
  engine/     transform.py (the pure transform) + models.py (CanonicalRow) + identity.py
  io/         reader.py (raw + device factors from landing) + writer.py (canonical parquet)
  config.py   pydantic-settings (env-prefixed SECHA_)
  cli.py      typer entrypoint
tests/        golden (vs secha-metadata) + self-contained unit tests
docs/         architecture diagram
```

## Build phases
- **Phase 1 (done):** pure-Python engine; **golden test green** against the `secha-metadata` contract;
  the sink writes a local **parquet** dataset (the columnar form Delta stores).
- **Phase 2 (done):** the engine applies `validation.yaml` — record rules (`not_null` → reject) run on
  the raw record, quantity rules (`range` → flag `suspect` / drop / reject) run on emitted values; every
  outcome is **counted** in the run stats (`TransformResult.stats`), and a declared rule the engine
  cannot honour **raises**. Remaining primitives are added as column families are mapped.
- **Phase 3:** write to **Delta / Unity Catalog** via Spark Connect on the TUNI cluster (the `spark`
  optional dependency), `MERGE` on `measurement_id`; build the wide `serving` views.

## Configuration
All settings are environment variables prefixed `SECHA_` (read from `.env`; see `.env.template`). None
are secrets — they are just paths.

| Variable | Default | Purpose |
|---|---|---|
| `SECHA_METADATA_ROOT` | `../secha-metadata` | the rulebook checkout the engine interprets |
| `SECHA_LANDING_ROOT` | `data/landing` | raw zone (usually `../secha-ingestion/data/landing`) |
| `SECHA_CANONICAL_ROOT` | `data/canonical` | Phase-1 local canonical parquet output |

## Develop & run
```bash
uv sync --dev
uv run pytest                       # golden (needs secha-metadata) + self-contained unit tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run secha-transform mx-electrix --date 2025-08-15 --meter 21   # raw landing -> canonical parquet
```
(No uv? `python -m venv .venv && .venv/Scripts/pip install -e . && .venv/Scripts/pip install pytest mypy ruff`,
then run the same commands without the `uv run` prefix.)

> A run only transforms what `secha-ingestion` has already **landed** for that date/meter. If nothing is
> landed you get `Transformed 0 record(s)` — land the day first with `secha-ingest`. Re-running the same
> date/meter **replaces** that run's output (run-scoped part files); when a partition holds several landed
> snapshots, the reader uses only the **latest** one (by the envelope's `fetched_at`).

The golden test reads the contract from `SECHA_METADATA_ROOT` (defaults to the sibling `secha-metadata`
checkout); the unit tests need nothing external.

## Adding a new vendor
No engine change. Add the vendor's config in `secha-metadata` (source schema, mapping, validation) and,
if needed, a CLI subcommand here. The engine already interprets any well-formed bundle. That "new vendor
= config, not code" property is the transform-side proof of the framework's decoupling claim.

## Status / open items
- **Scope:** MX Electrix `/measurements/` slice. Primitives implemented: unpivot, `none`,
  `scale_by_factor`, `parse_decimal` (locale separators), UTC timestamping, latest-snapshot selection,
  and **validation application** (flag/drop/reject with counted run stats). Unimplemented transforms
  and rules fail loudly. The full primitive set follows as column families are mapped.
- **Phase 3 (Delta/UC)** requires the TUNI VPN + a Unity Catalog token; canonical input for Spark must
  live on the cluster NFS (`/net/nfs`).
