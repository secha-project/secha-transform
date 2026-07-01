"""Runtime configuration via environment / .env (12-factor; env-prefixed SECHA_)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Engine settings. All keys are prefixed `SECHA_` (e.g. SECHA_METADATA_ROOT)."""

    model_config = SettingsConfigDict(env_prefix="SECHA_", env_file=".env", extra="ignore")

    metadata_root: str = "../secha-metadata"  # the secha-metadata checkout (the rulebook)
    landing_root: str = "data/landing"  # raw zone (Bronze), shared with secha-ingestion
    canonical_root: str = "data/canonical"  # Phase-1 local canonical parquet (Delta/UC comes later)
