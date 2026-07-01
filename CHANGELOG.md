# Changelog

All notable changes to `secha-transform` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: [SemVer](https://semver.org/).

## [Unreleased]

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
