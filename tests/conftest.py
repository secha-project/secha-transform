from __future__ import annotations

import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml


@pytest.fixture
def metadata_root() -> Path:
    """Resolve the secha-metadata checkout (the rulebook the engine is tested against).

    Uses SECHA_METADATA_ROOT if set, else the sibling repo. Skips if not found, so the
    self-contained unit tests still run in a bare environment.
    """
    env = os.environ.get("SECHA_METADATA_ROOT")
    root = Path(env) if env else (Path(__file__).resolve().parents[1].parent / "secha-metadata")
    root = root.resolve()
    if not (root / "canonical" / "canonical_schema.yaml").exists():
        pytest.skip(f"secha-metadata not found at {root}; set SECHA_METADATA_ROOT")
    return root


def load_fixture(metadata_root: Path, name: str) -> list[dict]:
    path = metadata_root / "tests" / "fixtures" / "mx_electrix" / name
    return json.loads(path.read_text(encoding="utf-8"))


def land_kempower_sample(metadata_root: Path, landing: Path) -> dict:
    """Land the Kempower golden sample as the ingestion layer would: one Parquet payload plus
    its envelope, in the partition the fixture names. Returns the raw fixture."""
    fixtures = metadata_root / "tests" / "fixtures" / "kempower"
    raw: dict = json.loads((fixtures / "raw_records_sample.json").read_text(encoding="utf-8"))
    source = yaml.safe_load(
        (metadata_root / "vendors" / "kempower" / "source_schema.yaml").read_text("utf-8")
    )
    partition = landing / source["access"]["layout"].format(**raw["partition"])
    partition.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(raw["records"]), partition / "0f1e2d3c4b5a6978.parquet")
    envelope = json.dumps({"fetched_at": "2026-09-24T00:00:00Z"})
    (partition / "0f1e2d3c4b5a6978.meta.json").write_text(envelope, encoding="utf-8")
    return raw
