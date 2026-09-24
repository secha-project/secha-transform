"""End to end: `secha-transform run <vendor>` from landed Parquet to canonical datasets."""

from __future__ import annotations

from pathlib import Path

import pyarrow.dataset as ds
import pytest
from typer.testing import CliRunner

from conftest import land_kempower_sample
from secha_transform.cli import app
from secha_transform.io.writer import ENTITY_PARTITIONING, MEASUREMENT_PARTITIONING


@pytest.fixture
def workspace(tmp_path: Path, metadata_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)  # no .env here: only the settings below apply
    monkeypatch.setenv("SECHA_METADATA_ROOT", str(metadata_root))
    monkeypatch.setenv("SECHA_LANDING_ROOT", str(tmp_path / "landing"))
    monkeypatch.setenv("SECHA_CANONICAL_ROOT", str(tmp_path / "canonical"))
    monkeypatch.setenv("SECHA_DIMENSIONS_ROOT", str(tmp_path / "dimensions"))
    land_kempower_sample(metadata_root, tmp_path / "landing")
    return tmp_path


def _tables(workspace: Path) -> tuple[ds.Dataset, ds.Dataset]:
    measurements = ds.dataset(str(workspace / "canonical"), partitioning=MEASUREMENT_PARTITIONING)
    sessions_root = str(workspace / "dimensions" / "charging_session")
    return measurements, ds.dataset(sessions_root, partitioning=ENTITY_PARTITIONING)


def test_run_transforms_a_vendor_from_its_metadata_alone(workspace: Path) -> None:
    result = CliRunner().invoke(app, ["run", "kempower"])

    assert result.exit_code == 0, result.output
    assert "6 records -> 24 rows" in result.output
    assert "2 distinct charging session(s)" in result.output
    measurements, sessions = _tables(workspace)
    assert measurements.count_rows() == 24
    assert set(measurements.to_table().column("event_date").to_pylist()) == {None}
    assert sessions.count_rows() == 2


def test_run_is_idempotent(workspace: Path) -> None:
    CliRunner().invoke(app, ["run", "kempower"])
    result = CliRunner().invoke(app, ["run", "kempower", "--select", "export=0f1e2d3c"])

    assert result.exit_code == 0, result.output
    measurements, sessions = _tables(workspace)
    assert (measurements.count_rows(), sessions.count_rows()) == (24, 2)


def test_run_refuses_a_key_the_layout_lacks(workspace: Path) -> None:
    result = CliRunner().invoke(app, ["run", "kempower", "--select", "date=2025-01-01"])
    assert result.exit_code != 0
    assert "layout's keys" in result.output


def test_run_says_so_when_nothing_matches(workspace: Path) -> None:
    result = CliRunner().invoke(app, ["run", "kempower", "--select", "export=ffffffff"])
    assert result.exit_code == 1
    assert "No landed partitions" in result.output


def test_run_refuses_a_vendor_that_needs_device_factors(workspace: Path) -> None:
    result = CliRunner().invoke(app, ["run", "mx_electrix"])
    assert result.exit_code == 1
    assert "device factors" in result.output


def test_a_session_across_batches_is_written_once_per_partition(workspace: Path) -> None:
    """Batch size 1 puts every record of a session in its own batch."""
    result = CliRunner().invoke(app, ["run", "kempower", "--batch-size", "1"])

    assert result.exit_code == 0, result.output
    measurements, sessions = _tables(workspace)
    assert measurements.count_rows() == 24
    assert sorted(sessions.to_table().column("session_id").to_pylist()) == [
        "kempower:session:" + "0a" * 32,
        "kempower:session:" + "0b" * 32,
    ]
