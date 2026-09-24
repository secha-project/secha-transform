# secha-transform

> The **deterministic, config-driven transform engine** for SECHA: raw vendor data → canonical.

`secha-transform` reads **raw** data (from the `secha-ingestion` landing zone) plus the **rulebook**
(`secha-metadata`) and produces **canonical** rows. It is a *deterministic interpreter of metadata*:
**no vendor logic lives in the engine**. Swap a config, get different output, zero code change. That
decoupling is the central interoperability claim of the thesis, demonstrated with two vendors end to
end, and tested on a third, Kempower, which needed new generic capabilities but no vendor logic.

## Architecture at a glance

![secha-transform data flow](docs/secha-transform-data-flow.svg)

Two inputs meet at the engine: **raw data** from `secha-ingestion` and the **rulebook** from
`secha-metadata`. The source schema's descriptors drive everything: `access.layout` resolves the
landing partitions, `format:` selects the parser (JSON or tab-separated triples), and `shape:`
selects the mapping style. Wide records (MX Electrix) are **unpivoted** into long rows while
transforms apply (scale by `uk`/`ik`, attach UTC); long records (ProCem) are resolved by a **keyed
lookup** in the mapping's `rows:` table (epoch-ms timestamps, per-row aggregation). Every row gets
a stable `measurement_id` and lands as canonical rows: **parquet locally now (Phase 1)**, Delta /
Unity Catalog with wide serving views later (Phase 3).

## Where this fits in the SECHA system
```
secha-ingestion  →  raw data (Bronze)
secha-metadata   →  the transformation rulebook (config-as-code)
secha-transform  →  reads raw + rulebook → canonical (Delta / Unity Catalog)   ← this repo
```

## What it does (two source shapes, one canonical form)
**Wide sources:** one record (e.g. ~240 MX Electrix columns) is **unpivoted into many long canonical
rows**, one per measured quantity. **Long sources:** each record is already one reading (a ProCem
`(rtl_id, value, epoch_ms)` triple); the mapping's `rows:` table gives it meaning. Either way each
canonical row is self-describing: `quantity · phase · variant · harmonic_order` + `value` + `unit`,
tagged with its source provenance and a deterministic `measurement_id`. The same physical thing
always takes the same row shape across vendors, which is what makes the data interoperable.

**Session sources:** a wide source whose rows belong to charging sessions and carry no clock time
(Kempower: 10-second steps of each session). Every row gets its `session_id` and its
`ts_session_offset_s`; `ts_utc` stays null, and so does `event_date`, stored in Hive's default
partition. Each distinct session also yields one `charging_session` row, built from the canonical
schema's own field list. With no row key in the source, a row's identity is its position in the
immutable landed Parquet part (`record.row_id_from: payload_position`).

**On the canonical shape (long vs wide).** Only `quantity` (with `value` and `unit`) carries meaning for
most variables. `phase` and `variant` sit at `none` and `harmonic_order` is `null` unless the row is a
phase-resolved or harmonic power-quality reading, so non-PQ data (a battery state-of-charge, a price)
only sets `quantity`. The long form is the **interoperability substrate**, not what consumers query:
Phase 3 builds **wide serving views** (one column per quantity) shaped per use case, so analysts get a
friendly wide table while the long form does the flexible plumbing underneath.

## Proven end-to-end (two vendors in one canonical table, a third transformed)
- **MX Electrix** (wide JSON API): a full real day, **1,440 one-minute records → ~36,000 canonical
  rows** (unpivot fan-out).
- **ProCem Kampusareena** (long 1 Hz file triples): a full real day, **14,476,804 records →
  5,499,568 canonical rows** (keyed lookup; 8,977,236 records for not-yet-mapped rtl_ids counted,
  never silent; mapped + unmapped = records in, exactly).
- **The convergence query**: one filter (`quantity=voltage, phase=L1`) returns both vendors in
  identical shape: `mx_electrix:meter:21 → 237.20 V` and `procem:kampusareena:evcharging →
  234.57 V`, same columns, same semantics. That single result is the interoperability claim, live.
- **Kempower** (wide Parquet, charging sessions, no clock time), transformed locally: all 99
  landed parts, **71,793,566 records -> 358,967,830 canonical rows** and 396,848 charging
  sessions in 2.3 h; reconciled against the raw parts independently (rows = non-empty cells per
  quantity, value sums equal, the 709 suspect readings = the export's 709 negative voltages).
  Not yet loaded into Unity Catalog.
- Golden tests assert the engine reproduces the exact canonical rows defined by the
  `secha-metadata` contract for **all three** vendors.

## Principles
- **Deterministic & pure.** `transform_records(records, bundle, factors)` is a pure function;
  same input + same config gives identical output (apart from the `ingested_at` stamp).
- **Vendor-blind.** The engine never contains a vendor name or `if vendor == …`; all vendor knowledge
  comes from the `secha-metadata` bundle it loads.
- **Idempotent.** `measurement_id` is a hash of the identity tuple, so re-runs MERGE safely.
- **Config is the contract.** The engine must satisfy the golden fixtures defined in `secha-metadata`.
- **Loud, never silent.** Declared transforms, formats, or rules the engine cannot honour raise;
  skipped or unmapped data is counted in the run stats, never dropped invisibly.

## Layout
```
src/secha_transform/
  metadata/   loader.py: load the rulebook into a MetadataBundle
  engine/     transform.py (the pure transform) + validation.py (rule application)
              + models.py (CanonicalRow, TransformResult) + identity.py
  io/         reader.py (descriptor-driven landing reader) + writer.py (canonical parquet)
  config.py   pydantic-settings (env-prefixed SECHA_)
  cli.py      typer entrypoint (one subcommand per vendor; batching for huge days)
tests/        golden per vendor (vs secha-metadata) + validation + IO + unit tests
experiments/  research tooling, outside the CI-gated contract (see llm_codegen/)
scripts/      reporting and platform helpers (canonical_sample.py, phase3/)
docs/         architecture diagram
```

## Build phases
- **Phase 1 (done):** pure-Python engine; **golden tests green** against the `secha-metadata`
  contract; the sink writes a local **parquet** dataset (the columnar form Delta stores).
- **Phase 2 (done):** the engine applies `validation.yaml`: record rules (`not_null` → reject) run on
  the raw record, quantity rules (`range` → flag `suspect` / drop / reject) run on emitted values; every
  outcome is **counted** in the run stats (`TransformResult.stats`), and a declared rule the engine
  cannot honour **raises**. Remaining primitives are added as column families are mapped.
- **Phase 3 (done, live on the TUNI cluster):** Delta / Unity Catalog via Spark Connect (the `spark`
  extra, pinned `pyspark-client==4.1.1`). `io/delta_sink.py` generates the table DDL from
  `canonical_schema.yaml` + the target's `table_properties` (incl. the platform-required
  `delta.feature.catalogManaged`), reads cluster-visible staging parquet, dedupes on the merge key
  (latest `ingested_at` wins), and `MERGE`s on `measurement_id`. Serving definitions come from the
  rulebook's `serving/*.sql` (`{canonical}` placeholder), materialised per the target's
  `serving_mode` (Delta snapshots here: this UC connector lacks views and RTAS). Reference
  dimensions (e.g. `secha.canonical.quantity`) are published from the rulebook vocabularies so
  consumers JOIN the long fact for descriptions + standards.
  CLI: `delta-load`, `delta-views` (dimensions + serving), and `--sink delta` on the vendor commands.
  **Verified live:** `secha.canonical.measurement` holds 5,535,568 rows (both vendors; re-running
  the load reports `5535568 -> 5535568`, the platform-level idempotency proof) and
  `secha.serving.pq_minute_wide` answers the convergence query. Full record:
  [docs/phase3-log.md](docs/phase3-log.md).

## Configuration
All settings are environment variables prefixed `SECHA_` (read from `.env`; see `.env.template`). None
are secrets; they are just paths.

| Variable | Default | Purpose |
|---|---|---|
| `SECHA_METADATA_ROOT` | `../secha-metadata` | the rulebook checkout the engine interprets |
| `SECHA_LANDING_ROOT` | `data/landing` | raw zone (usually `../secha-ingestion/data/landing`) |
| `SECHA_CANONICAL_ROOT` | `data/canonical` | Phase-1 local canonical parquet output |
| `SECHA_DIMENSIONS_ROOT` | `data/canonical-dimensions` | other canonical entities, one dataset each (e.g. `charging_session/`) |
| `SECHA_SPARK_URL` | unset | Phase 3: Spark Connect endpoint (TUNI VPN) |
| `SECHA_CATALOG_URL` | unset | Phase 3: Unity Catalog API |
| `SECHA_CATALOG_TOKEN` | unset | Phase 3: UC token (secret; `.env` only) |
| `SECHA_STAGING_ROOT` | unset | Phase 3: cluster-visible staging path for canonical parquet |

## Develop & run
```bash
uv sync --dev
uv run pytest                       # golden (needs secha-metadata) + self-contained unit tests
uv run pytest experiments/llm_codegen/tests   # the LLM experiment's own tests (not run in CI)
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run secha-transform mx-electrix --date 2025-08-15 --meter 21   # raw landing -> canonical parquet
uv run secha-transform procem --date 2026-06-15                   # streams + batches 14.5M records
uv run secha-transform run kempower                               # any vendor, from its metadata alone
uv run secha-transform run kempower --select part=00000-c000      # one partition (layout key=value)

# Phase 3 (needs the spark extra + .env platform values + TUNI VPN):
uv run secha-transform delta-load --staging /net/nfs/data/secha/canonical-staging/load-001
uv run secha-transform delta-views                                # publish reference dimensions + serving snapshots
```
(No uv? `python -m venv .venv && .venv/Scripts/pip install -e . && .venv/Scripts/pip install pytest mypy ruff`,
then run the same commands without the `uv run` prefix.)

> A run only transforms what `secha-ingestion` has already **landed** for the selected partitions. If
> nothing is landed you get `Transformed 0 record(s)` (or, with `run`, a clear refusal); land the data
> first with `secha-ingest`. Re-running the same partitions **replaces** that run's output (run-scoped
> part files); when a partition holds several landed snapshots, the reader uses only the **latest** one
> (by the envelope's `fetched_at`).

Every output file has the same explicit schema, whatever nulls a batch holds, and the datasets are
read with the partitioning the writer declares (`MEASUREMENT_PARTITIONING`, `ENTITY_PARTITIONING`
in `io/writer.py`): a dataset holding only rows without clock time has no non-null `event_date` for
inference to type, and pyarrow's inference then fails.

The golden tests read the contract from `SECHA_METADATA_ROOT` (defaults to the sibling `secha-metadata`
checkout); the unit tests need nothing external.

## Showing the output to a reader

`scripts/canonical_sample.py` runs the engine over a minute of landed data per vendor and
writes one workbook: the canonical rows, the same rows through `serving/pq_minute_wide.sql`,
the run statistics, and the field and vocabulary dictionaries. Nothing in it is written by hand,
so it shows what the engine does today rather than what a document once claimed.

```bash
uv pip install openpyxl                                 # a reporting need, not an engine one
python scripts/canonical_sample.py --out ../review
```

The sample holds real readings, so the workbook states on its first sheet that it is
project-internal. The wide sheet is produced by executing the rulebook's SELECT in SQLite, with
`date_trunc` replaced by an equivalent expression, and the workbook records that substitution.

## Adding a new vendor
No vendor logic, and no CLI change: add the vendor's config in `secha-metadata` (source schema,
mapping, validation) and run it with `secha-transform run <vendor>`. The engine interprets any
well-formed bundle, wide or long, dated or session-based. A source unlike any before may first need
a **generic** capability: ProCem needed three (long records, epoch timestamps, descriptor-driven
reading) and Kempower five (Parquet, any layout placeholders, a positional row id, sessions with
`charging_session` rows, and rows without a date, plus per-column aggregation). Neither added a
line of vendor logic; the costs are measured in `secha-metadata`'s onboarding diaries.

## Status / open items
- **Scope:** three vendors: MX Electrix (wide JSON), ProCem Kampusareena (long DSV triples) and
  Kempower (wide Parquet, charging sessions, no clock time).
  Primitives implemented: unpivot (wide), **keyed `rows:` lookup (long)** with per-row and
  per-column aggregation overrides, `none`, `scale_by_factor`, `parse_decimal`, timestamps
  (`iso8601`, **`epoch_ms`/`epoch_s`** with exact integer math), descriptor-driven reading
  (`access.layout` with any placeholders + `format:` incl. header-less DSV and **Parquet**),
  latest-snapshot selection, positional row ids, **sessions** with `charging_session` rows, rows
  without a date, streaming/batched processing, and **validation application** (flag/drop/reject
  with counted run stats). Unimplemented transforms, formats, and rules fail loudly. No vendor
  name appears in the engine or IO layers; the CLI names only the first two vendors' own commands.
- **Kempower in Unity Catalog is the next step.** The local canonical output exists; loading it
  needs a Spark-side check that the null `event_date` partition reads as null, and a MERGE for the
  `charging_session` table, which the sink does not have yet.
- **Phase 3 is live.** Operational notes: platform commands need the TUNI VPN + a Unity Catalog token
  in `.env`; staged canonical parquet must sit on the cluster NFS (`/net/nfs`); serving snapshots are
  refreshed by re-running `delta-views` after each `delta-load`. Platform runbook + facts learned:
  [scripts/phase3/README.md](scripts/phase3/README.md) and [docs/phase3-log.md](docs/phase3-log.md).
