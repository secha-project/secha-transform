"""`secha-transform delta-load` argument checks that need no cluster (no pyspark, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from secha_transform.cli import app


def test_delta_load_refuses_an_undeclared_entity_before_connecting(
    tmp_path: Path, metadata_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here: nothing could connect even if it tried
    monkeypatch.setenv("SECHA_METADATA_ROOT", str(metadata_root))

    result = CliRunner().invoke(app, ["delta-load", "--staging", "/nowhere", "--entity", "tariff"])

    assert result.exit_code == 2, result.output  # a usage error, not a platform failure
    message = " ".join(result.output.replace("│", " ").split())  # unwrap the error box
    assert "entity 'tariff' is neither the fact table nor a declared dimension" in message
