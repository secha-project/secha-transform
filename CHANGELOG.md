# Changelog

All notable changes to `secha-transform` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: [SemVer](https://semver.org/).

## [Unreleased]
### Fixed
- Reader now selects the **latest landed snapshot** per partition (by the envelope's `fetched_at`),
  implementing the source schema's `snapshot_selection`. Previously all snapshots were read, producing
  duplicate — or, for revised data, contradictory — records.
- Reader decodes raw payloads as explicit UTF-8 (text mode used the platform encoding — cp1252 on
  Windows — and would misdecode e.g. Finnish characters).
- Timestamp handling: timezone-aware values with negative offsets (`-05:00`) pass through unchanged
  (previously got a fabricated `Z` appended); unparseable timestamps yield null instead of garbage.
### Added
- `parse_decimal` transform (locale decimal separators — the documented PQ-analyzer CSV pain point).
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
