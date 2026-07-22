# Changelog

All notable changes to `secha-transform` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: [SemVer](https://semver.org/).

## [Unreleased]
### Added (Phase 3, Step 2: the Delta / Unity Catalog sink)
- `io/delta_sink.py`: pure SQL builders (offline-tested) + a thin Spark Connect wrapper.
  Table DDL is GENERATED from `canonical_schema.yaml` and the target binding, including the
  platform-required `TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported')`; staged
  parquet is read from a cluster-visible path, typed per the canonical schema (missing
  engine columns become typed NULLs), deduplicated on the merge key (latest `ingested_at`
  wins), and MERGEd on `measurement_id`. Serving views are created from the rulebook's
  `serving/*.sql` via the `{canonical}` placeholder. Unknown canonical types raise.
- CLI: `delta-load` (staged parquet -> MERGE, counted report), `delta-views`, and
  `--sink delta` on both vendor commands (for runs where the canonical output path is
  cluster-visible). Missing platform env produces a clean error, never a traceback.
- Serving materialisation is config-driven (`serving_mode` in the target binding): `view`,
  or `table` for platforms whose catalog connector lacks view/RTAS abilities (this UC
  connector lacks both), refreshed via drop + explicit DDL from the analysed SELECT +
  insert-select, using only platform-proven primitives.
- **Reference dimension tables** published from the rulebook vocabularies (`reference_dimensions`
  in the target binding): `secha.canonical.quantity` (quantity, default_unit, standard_ref,
  description) so downstream consumers JOIN the long fact for human-readable semantics and
  standards, rather than relying on column comments (the correct home for meaning in a long
  model, and it sidesteps the UC 0.4 comment limitation). Built with the same proven primitives
  (drop + DDL-with-comments + insert-values, single quotes escaped). `delta-views` now publishes
  dimensions then serving views. Deterministic (keys sorted); every builder offline-tested.
- Settings + `.env.template`: `SECHA_SPARK_URL`, `SECHA_CATALOG_URL`, `SECHA_CATALOG_TOKEN`
  (secret), `SECHA_STAGING_ROOT`. The `spark` extra is now `pyspark-client==4.1.1`, pinned
  to the platform's Spark version.
- Tests: offline builder tests pin every generated SQL statement + an env-gated integration
  test (real platform, throwaway table, MERGE twice for idempotency, self-cleaning;
  auto-skipped without the Phase-3 env).
- **Verified live on the TUNI cluster (2026-07-15):** both proven days staged to NFS and
  MERGEd into `secha.canonical.measurement` (5,535,568 rows; second load `5535568 ->
  5535568`, idempotent); `secha.serving.pq_minute_wide` built and answering the
  cross-vendor convergence query. Platform facts recorded in `docs/phase3-log.md`.
### Added (vendor #2, ProCem: three generic capabilities, zero vendor logic)
- **Long-shape sources** (`shape: long`): mapping `rows:` entries are resolved by the record's
  key-field VALUE (one record → one canonical row), with per-row `aggregation` override (energy
  counters) and explicit `harmonic_order`. Unmapped keys are counted (`records_unmapped`), never
  silently dropped; `meter_field` is now optional (long sources have no meter concept).
- **Timestamp formats** per the source's `format.datetime_format`: `epoch_ms`/`epoch_s` join
  `iso8601`, rendered with integer math (milliseconds can never float-round); unknown formats raise.
- **Descriptor-driven reader**: partitions resolve from `access.layout`, parsing from `format:`
  (JSON + header-less DSV; DSV values stay strings, since interpretation is the engine's job, driven by
  the mapping). Records now stream lazily (`Iterator`); **breaking:** `read_measurements` replaced
  by `read_records(landing_root, source_schema, date, meter=None)`.
- CLI `secha-transform procem --date …`: streams + batches multi-million-row days
  (`--batch-size`, default 500k) with run-scoped idempotent writes per batch.
- Golden tests for the ProCem contract (real 2026-06-15 values: Helsinki-midnight timestamp,
  fundamental/Fryze variants, harmonic order, counter aggregation) + 7 new unit/IO tests.
- Live, both vendors in one canonical table: MX Electrix 1,440 records → 36,000 rows; ProCem
  14,476,804 records → 5,499,568 rows + 8,977,236 unmapped-id records counted (sum reconciles
  exactly); one query returns voltage L1 from both vendors in identical shape.
### Added (Phase 2)
- The engine now applies the vendor's `validation.yaml` (the fifth metadata type): record-level
  `not_null` rules reject raw records; quantity-level `range` rules flag emitted rows `suspect`
  (value kept), drop single rows, or reject the whole record. Declared rules the engine cannot
  honour raise instead of being silently ignored.
- `transform_records` now returns `TransformResult(rows, stats)`: run statistics (records in/rejected,
  rows emitted/suspect/dropped, null cells skipped) are first-class and reported by the CLI.
  **Breaking:** callers previously received a plain row list.
### Fixed
- Reader now selects the **latest landed snapshot** per partition (by the envelope's `fetched_at`),
  implementing the source schema's `snapshot_selection`. Previously all snapshots were read, producing
  duplicate (or, for revised data, contradictory) records.
- Reader decodes raw payloads as explicit UTF-8 (text mode used the platform encoding, cp1252 on
  Windows, and would misdecode e.g. Finnish characters).
- Timestamp handling: timezone-aware values with negative offsets (`-05:00`) pass through unchanged
  (previously got a fabricated `Z` appended); unparseable timestamps yield null instead of garbage.
### Added
- `parse_decimal` transform (locale decimal separators, the documented PQ-analyzer CSV pain point).
- Unimplemented transforms now raise a clear error instead of silently falling back to `float(raw)`.
- Writer idempotency: run-scoped part-file names + overwrite, so re-running a date/meter replaces its
  own output without touching other scopes sharing the partition.
- One `ingested_at` stamp per run (rows of a batch share their provenance instant).
- CLI: `--date` validated up front; clean error when the metadata root is missing.
### Changed
- The measurement identity hash now includes `aggregation`, matching the updated
  `measurement_id_from` in secha-metadata (full identity tuple; prevents future min/max/mean
  collisions in the merge key).

## [0.1.0] - 2026-06-18
### Added
- Deterministic, vendor-blind transform engine (`engine/transform.py`): wide source record → long
  canonical rows, driven entirely by the `secha-metadata` bundle.
- Metadata loader (`metadata/loader.py`) reading the canonical layer + a vendor's config.
- Primitives: unpivot, `none`, `scale_by_factor` (per-device uk/ik), UTC timestamping; deterministic
  `measurement_id` (idempotent / MERGE-safe).
- IO: landing-zone reader (raw records + device factors) and Phase-1 parquet writer.
- Typer CLI (`secha-transform mx-electrix`), pydantic-settings config, structlog logging.
- Tests: golden test against the `secha-metadata` contract + self-contained unit tests; ruff +
  mypy(strict) + pytest + pre-commit + CI.
