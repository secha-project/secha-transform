"""`secha-transform delta-load` argument checks that need no cluster (no pyspark, no network)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from secha_transform.cli import app

# Typer forces colour when GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set, as on CI, and it
# decides this on import; so the test reads the message with any colour codes taken out.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_delta_load_refuses_an_undeclared_entity_before_connecting(
    tmp_path: Path, metadata_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here: nothing could connect even if it tried
    monkeypatch.setenv("SECHA_METADATA_ROOT", str(metadata_root))

    result = CliRunner().invoke(app, ["delta-load", "--staging", "/nowhere", "--entity", "tariff"])

    assert result.exit_code == 2, result.output  # a usage error, not a platform failure
    plain = _ANSI.sub("", result.output)
    message = " ".join(plain.replace("│", " ").split())  # unwrap the error box
    assert "entity 'tariff' is neither the fact table nor a declared dimension" in message
