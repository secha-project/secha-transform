"""Frozen evaluation cases: an input, and the engine's output for that input.

Three splits, each with its own rule about where its data may go:

- dev: synthetic records with fabricated values. This is the only data ever placed in a
  prompt or in repair feedback. The approval to use commercial endpoints covers partner
  metadata, not measurements, so no measured value may leave this machine.
- test: real records sampled from the landing zone, plus edge cases. Used only inside the
  local sandbox, and never sent to any model.
- drift: a test input scored against a rulebook that has been edited after the program was
  written, to measure whether a program follows a configuration change it has never seen.

Sampling is systematic rather than random, so the same landing zone always yields the same
cases without a seed to record.
"""

from __future__ import annotations

import copy
import itertools
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from codegen.oracle import rulebook_from_bundle, run_engine
from secha_transform.io.reader import parse_dsv_records, read_device_factors, read_records
from secha_transform.metadata.loader import load_bundle

VENDORS = ("mx_electrix", "procem_kampusareena_pq")
MODES = ("snapshot", "interpreter")

Mutation = Callable[[dict[str, Any]], None]


@dataclass
class Case:
    """One input and the engine's output for it."""

    vendor: str
    name: str
    split: str
    records: list[dict[str, Any]]
    device_factors: dict[str, dict[str, float]]
    rulebook: dict[str, Any]
    expected_rows: list[dict[str, Any]]
    expected_stats: dict[str, int]
    real_data: bool
    mutation: str | None = None
    description: str = ""

    @property
    def case_id(self) -> str:
        return f"{self.vendor}/{self.split}/{self.name}"

    def payload(self, mode: str) -> dict[str, Any]:
        """What the program reads on standard input."""
        data: dict[str, Any] = {"records": self.records, "device_factors": self.device_factors}
        if mode == "interpreter":
            data["rulebook"] = self.rulebook
        return data


def _make(
    metadata_root: Path,
    rulebook: dict[str, Any],
    name: str,
    split: str,
    records: list[dict[str, Any]],
    factors: dict[str, dict[str, float]],
    real_data: bool,
    description: str,
    mutation: str | None = None,
) -> Case:
    rows, stats = run_engine(metadata_root, rulebook, records, factors)
    return Case(
        vendor=rulebook["vendor"],
        name=name,
        split=split,
        records=copy.deepcopy(records),
        device_factors=copy.deepcopy(factors),
        rulebook=copy.deepcopy(rulebook),
        expected_rows=rows,
        expected_stats=stats,
        real_data=real_data,
        mutation=mutation,
        description=description,
    )


# ------------------------------------------------------------------------- fabricated values

# Plausible magnitudes with deliberately unusual decimals, so a fabricated value can never be
# mistaken for a measured one and a leak into a prompt would be detectable by string search.
_FABRICATED = {
    "frequency": 50.0123,
    "voltage": 115.4321,
    "current": 1.2345,
    "thd_voltage": 1.0987,
    "thd_current": 3.2109,
    "power_factor": 0.9543,
    "displacement_power_factor": 0.9678,
    "voltage_unbalance_negative_seq": 0.3456,
    "voltage_unbalance_zero_seq": 0.1234,
    "harmonic_voltage": 0.4321,
    "harmonic_current": 2.3456,
    "active_power": 1234.5678,
    "reactive_power": -234.5678,
    "apparent_power": 1345.6789,
    "energy_active_import": 123456.7891,
    "energy_active_export": 2345.6781,
    "energy_reactive_import": 34567.8912,
    "energy_reactive_export": 4567.8913,
}


def _fabricated(quantity: str, index: int) -> float:
    # A relative step, so a ratio such as a power factor never drifts past its physical limit
    # and only the edge cases a development case sets on purpose trip a validation rule.
    return round(_FABRICATED.get(quantity, 12.3456) * (1 + 0.0011 * index), 4)


def _first(items: list[dict[str, Any]], test: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    return next(item for item in items if test(item))


# -------------------------------------------------------------------------------- MX Electrix


def _mx_record(
    rulebook: dict[str, Any], meter: int, row_id: int, timestamp: str | None
) -> dict[str, Any]:
    mapping = rulebook["mapping"]
    record: dict[str, Any] = {"id": row_id, "meter": meter, "timestamp": timestamp}
    for index, column in enumerate(mapping["columns"]):
        record[column["src"]] = _fabricated(column["quantity"], index)
    for rule in mapping.get("generated", []):
        # order 9 is present in the record but not in the mapping: a field to ignore
        for step, order in enumerate([*rule["order"], 9]):
            for position in rule["phase_map"]:
                field = rule["pattern"].format(order=order, p=position)
                record[field] = _fabricated(rule["quantity"], step * 3 + int(position))
    record["pw"] = 3.1415  # an unmapped field of another kind
    return record


def _mx_dev(metadata_root: Path, rulebook: dict[str, Any]) -> Case:
    columns = rulebook["mapping"]["columns"]
    frequency = _first(columns, lambda c: c["quantity"] == "frequency")["src"]
    thd = [c["src"] for c in columns if c["quantity"] == "thd_voltage"][1]
    power_factor = _first(columns, lambda c: c["quantity"] == "power_factor")["src"]
    rule = rulebook["mapping"]["generated"][0]
    harmonic = rule["pattern"].format(order=rule["order"][1], p=list(rule["phase_map"])[1])

    # Three records keep the example inside a 16k-token context and still exercise every
    # rule: scaling with a UTC offset, a meter without factors, nulls, both range checks and a
    # rejected record.
    scaled = _mx_record(rulebook, 901, 900001, "2024-01-10T12:00:00+02:00")
    scaled[power_factor] = 1.4321
    unscaled = _mx_record(rulebook, 902, 900002, "2024-01-10T12:01:00")
    unscaled.update({frequency: 71.2345, thd: None, harmonic: None})
    no_time = _mx_record(rulebook, 901, 900003, None)

    return _make(
        metadata_root,
        rulebook,
        "synthetic",
        "dev",
        [scaled, unscaled, no_time],
        {"901": {"uk": 2.0, "ik": 10.0}},
        real_data=False,
        description=(
            "Fabricated values. A scaled meter with a UTC offset and a power factor out of "
            "range; a meter without factors carrying a null cell, a null generated cell and a "
            "frequency out of range; a record without a timestamp."
        ),
    )


def _mx_tests(
    metadata_root: Path, rulebook: dict[str, Any], landing_root: Path | None
) -> list[Case]:
    cases: list[Case] = []
    base: dict[str, Any] | None = None
    if landing_root is not None:
        bundle = load_bundle(metadata_root, "mx_electrix")
        partitions = landing_root / "vendor=mx_electrix" / "source=measurements"
        date = sorted(p.name.split("=", 1)[1] for p in partitions.glob("date=*"))[0]
        meters = sorted(
            p.name.split("=", 1)[1] for p in (partitions / f"date={date}").glob("meter=*")
        )
        factors = read_device_factors(str(landing_root), "mx_electrix", date, bundle)
        for meter in meters:
            day = list(read_records(str(landing_root), bundle.source_schema, date, meter))
            picks = sorted({*range(0, len(day), 120), len(day) - 1})
            sample = [day[i] for i in picks]
            base = base or copy.deepcopy(sample[0])
            cases.append(
                _make(
                    metadata_root,
                    rulebook,
                    f"real_meter_{meter}",
                    "test",
                    sample,
                    factors,
                    real_data=True,
                    description=f"{len(sample)} real records spread across {date}, meter {meter}.",
                )
            )
        edge_factors = factors
    else:
        edge_factors = {"901": {"uk": 2.0, "ik": 10.0}, "22": {"uk": 1.0, "ik": 1.0}}
    real = base is not None
    base = base or _mx_record(rulebook, 901, 900100, "2024-01-10T13:00:00")
    meter = base["meter"]

    columns = rulebook["mapping"]["columns"]
    frequency = _first(columns, lambda c: c["quantity"] == "frequency")["src"]
    voltages = [c["src"] for c in columns if c["quantity"] == "voltage"]
    thd = _first(columns, lambda c: c["quantity"] == "thd_voltage")["src"]
    power_factor = _first(columns, lambda c: c["quantity"] == "power_factor")["src"]
    rule = rulebook["mapping"]["generated"][0]
    harmonic = rule["pattern"].format(order=rule["order"][0], p=next(iter(rule["phase_map"])))

    changes: list[dict[str, Any]] = [
        {"timestamp": None},
        {"meter": None},
        {voltages[1]: None, thd: None},
        {frequency: 70.5},
        {power_factor: 1.7},
        {"meter": 999},
        {"timestamp": "2025-08-15T12:00:00+03:00"},
        {"timestamp": "15/08/2025 12:00"},
        {"meter": 22, voltages[0]: 2000.0},
        {harmonic: None},
    ]
    edges = []
    for number, change in enumerate(changes):
        record = copy.deepcopy(base)
        record.update(change)
        record["id"] = int(base["id"]) + 1_000_000 + number
        edges.append(record)
    cases.append(
        _make(
            metadata_root,
            rulebook,
            "edge_records",
            "test",
            edges,
            edge_factors,
            real_data=real,
            description=(
                f"Edge cases derived from one record of meter {meter}: no timestamp, no meter, "
                "null cells, values out of range, a meter without factors, a UTC offset, an "
                "unparseable timestamp, a large scaled voltage and a null generated cell."
            ),
        )
    )
    return cases


# ------------------------------------------------------------------------------------ ProCem


def _procem_fields(rulebook: dict[str, Any]) -> tuple[str, str, str]:
    record = rulebook["source_schema"]["record"]
    return record["key_field"], record["value_field"], record["timestamp_field"]


def _first_unmapped_key(rulebook: dict[str, Any]) -> str:
    keys = {int(row["key"]) for row in rulebook["mapping"]["rows"]}
    return str(next(k for k in range(min(keys), max(keys)) if k not in keys))


def _procem_dev(metadata_root: Path, rulebook: dict[str, Any]) -> Case:
    rows = rulebook["mapping"]["rows"]
    key, value, stamp = _procem_fields(rulebook)
    frequency = _first(rows, lambda r: r["quantity"] == "frequency")["key"]
    line_neutral = _first(
        rows,
        lambda r: (
            r["quantity"] == "voltage"
            and r["phase"] in {"L1", "L2", "L3"}
            and r.get("variant", "none") == "none"
        ),
    )["key"]
    line_line = _first(rows, lambda r: r["quantity"] == "voltage" and "_" in r["phase"])["key"]
    fundamental = _first(rows, lambda r: r.get("variant") == "fundamental")
    fryze = _first(rows, lambda r: r.get("variant") == "fryze")
    harmonic = _first(rows, lambda r: r.get("harmonic_order") is not None)["key"]
    counter = _first(rows, lambda r: r.get("aggregation") == "counter")["key"]

    def triple(k: str | None, v: str | None, t: str | None) -> dict[str, Any]:
        return {key: k, value: v, stamp: t}

    records = [
        triple(frequency, "50.0123", "1704888000000"),
        triple(line_neutral, "231.0456", "1704888000000"),
        triple(line_line, "400.1234", "1704888000000"),
        triple(
            fundamental["key"], f"{_fabricated(fundamental['quantity'], 7):.4f}", "1704888000999"
        ),
        triple(fryze["key"], f"{_fabricated(fryze['quantity'], 5):.4f}", "1704888000999"),
        triple(harmonic, "0.4321", "1704888001000"),
        triple(counter, "123456.7891", "1704888001000"),
        triple(_first_unmapped_key(rulebook), "7.7777", "1704888001000"),
        triple(line_neutral, "1100.5432", "1704888002000"),
        triple(frequency, "49.9876", None),
        triple(None, "1.0001", "1704888002000"),
        triple(frequency, "49.9765", "1.704888e12"),
    ]
    return _make(
        metadata_root,
        rulebook,
        "synthetic",
        "dev",
        records,
        {},
        real_data=False,
        description=(
            "Fabricated string triples: frequency, voltages, fundamental and Fryze variants, a "
            "harmonic, an energy counter, an unmapped key, a voltage out of range, a record "
            "without a timestamp, a record without a key and a non-integer epoch."
        ),
    )


def _latest_payload(directory: Path) -> Path:
    payloads = [
        p
        for p in directory.iterdir()
        if p.is_file() and not p.name.endswith((".meta.json", ".tmp"))
    ]
    if not payloads:
        raise FileNotFoundError(f"no landed payload in {directory}")

    def recency(path: Path) -> tuple[str, str]:
        meta = path.with_name(path.name.rsplit(".", 1)[0] + ".meta.json")
        fetched = (
            json.loads(meta.read_text(encoding="utf-8")).get("fetched_at") if meta.exists() else ""
        )
        return (str(fetched or ""), str(path))

    return max(payloads, key=recency)


def _spread_lines(path: Path, count: int) -> list[bytes]:
    """Lines at evenly spaced byte offsets: a whole-day sample without reading the whole day."""
    size = path.stat().st_size
    lines: list[bytes] = []
    with path.open("rb") as handle:
        for index in range(count):
            handle.seek(index * size // count)
            if index:
                handle.readline()  # the seek landed mid-line; skip to the next full one
            line = handle.readline()
            if line.strip():
                lines.append(line if line.endswith(b"\n") else line + b"\n")
    return lines


def _procem_tests(
    metadata_root: Path, rulebook: dict[str, Any], landing_root: Path | None
) -> list[Case]:
    cases: list[Case] = []
    if landing_root is not None:
        bundle = load_bundle(metadata_root, "procem_kampusareena_pq")
        partitions = landing_root / "vendor=procem_kampusareena_pq" / "source=daily_dump"
        date = sorted(p.name.split("=", 1)[1] for p in partitions.glob("date=*"))[0]
        payload = _latest_payload(partitions / f"date={date}")
        with payload.open("rb") as handle:
            start = list(itertools.islice(handle, 200))
        for name, lines, description in (
            (
                "real_day_start",
                start,
                f"The first 200 lines of {date}: the local-midnight boundary, every id.",
            ),
            (
                "real_day_spread",
                _spread_lines(payload, 400),
                f"400 lines at even offsets across {date}.",
            ),
        ):
            records = list(parse_dsv_records(b"".join(lines), bundle.source_schema))
            cases.append(
                _make(
                    metadata_root,
                    rulebook,
                    name,
                    "test",
                    records,
                    {},
                    real_data=True,
                    description=description,
                )
            )

    rows = rulebook["mapping"]["rows"]
    key, value, stamp = _procem_fields(rulebook)
    frequency = _first(rows, lambda r: r["quantity"] == "frequency")["key"]
    voltage = _first(rows, lambda r: r["quantity"] == "voltage")["key"]
    power_factor = _first(
        rows, lambda r: r["quantity"] in {"power_factor", "displacement_power_factor"}
    )["key"]
    counter = _first(rows, lambda r: r.get("aggregation") == "counter")["key"]
    edges = [
        {key: frequency, value: "50.0012", stamp: None},
        {key: None, value: "231.1111", stamp: "1781470800000"},
        {key: voltage, value: None, stamp: "1781470800000"},
        {key: "99999", value: "1.2345", stamp: "1781470800000"},
        {key: voltage, value: "1200.75", stamp: "1781470800001"},
        {key: frequency, value: "44.1", stamp: "1781470800002"},
        {key: power_factor, value: "-1.5", stamp: "1781470800003"},
        {key: frequency, value: "50.0001", stamp: "1781470800000"},
        {key: frequency, value: "49.9999", stamp: "1781470800999"},
        {key: frequency, value: "50.0002", stamp: "1.781470800e12"},
        {key: counter, value: "98765.4321", stamp: "1781470801000"},
    ]
    cases.append(
        _make(
            metadata_root,
            rulebook,
            "edge_records",
            "test",
            edges,
            {},
            real_data=False,
            description=(
                "Fabricated triples on mapped keys: no timestamp, no key, a null value, an "
                "unknown key, values out of range, millisecond boundaries, a non-integer epoch "
                "and an energy counter."
            ),
        )
    )
    return cases


# ------------------------------------------------------------------------------------- drift


def _edited(rulebook: dict[str, Any], change: Mutation) -> dict[str, Any]:
    edited = copy.deepcopy(rulebook)
    change(edited)
    return edited


def _set_range_max(quantity: str, maximum: float) -> Mutation:
    def change(rulebook: dict[str, Any]) -> None:
        for rule in rulebook["validation"]["rules"]:
            if rule.get("quantity") == quantity and rule["type"] == "range":
                rule["max"] = maximum

    return change


def _mutations(rulebook: dict[str, Any]) -> list[tuple[str, str, Mutation]]:
    """Ordinary configuration edits, each of which must change the engine's output."""
    if rulebook["source_schema"].get("shape", "wide") == "wide":
        power_factors = [
            c["src"] for c in rulebook["mapping"]["columns"] if c["quantity"] == "power_factor"
        ]
        dropped = power_factors[-1]

        def add_order(rb: dict[str, Any]) -> None:
            rb["mapping"]["generated"][0]["order"].append(9)

        def remove_column(rb: dict[str, Any]) -> None:
            rb["mapping"]["columns"] = [c for c in rb["mapping"]["columns"] if c["src"] != dropped]

        return [
            (
                "add_harmonic_order_9",
                "The 9th voltage harmonic is added to the generated rule.",
                add_order,
            ),
            (
                f"remove_column_{dropped}",
                f"The {dropped} column is removed from the mapping.",
                remove_column,
            ),
            (
                "tighten_frequency_max_50",
                "The frequency range maximum drops from 65 to 50.",
                _set_range_max("frequency", 50.0),
            ),
        ]

    unmapped = _first_unmapped_key(rulebook)
    frequency = _first(rulebook["mapping"]["rows"], lambda r: r["quantity"] == "frequency")["key"]

    def map_key(rb: dict[str, Any]) -> None:
        rb["mapping"]["rows"].append(
            {
                "key": unmapped,
                "quantity": "harmonic_voltage",
                "phase": "L1",
                "harmonic_order": 2,
                "unit": "percent",
            }
        )

    def unmap_frequency(rb: dict[str, Any]) -> None:
        rb["mapping"]["rows"] = [r for r in rb["mapping"]["rows"] if r["key"] != frequency]

    return [
        (
            f"map_key_{unmapped}",
            f"Previously unmapped key {unmapped} is mapped as a 2nd voltage harmonic.",
            map_key,
        ),
        (
            "unmap_frequency",
            f"The frequency entry (key {frequency}) is removed from the mapping.",
            unmap_frequency,
        ),
        (
            "tighten_voltage_max_235",
            "The voltage range maximum drops from 1000 to 235.",
            _set_range_max("voltage", 235.0),
        ),
    ]


# ------------------------------------------------------------------------------------- build


def build_cases(metadata_root: Path, landing_root: Path | None) -> list[Case]:
    """Every case for every vendor. Without a landing zone, only synthetic cases are built."""
    builders = {
        "mx_electrix": (_mx_dev, _mx_tests),
        "procem_kampusareena_pq": (_procem_dev, _procem_tests),
    }
    cases: list[Case] = []
    for vendor in VENDORS:
        rulebook = rulebook_from_bundle(load_bundle(metadata_root, vendor))
        dev_builder, test_builder = builders[vendor]
        dev = dev_builder(metadata_root, rulebook)
        tests = test_builder(metadata_root, rulebook, landing_root)
        cases.append(dev)
        cases.extend(tests)

        source = next((c for c in tests if c.real_data), dev)
        for name, description, change in _mutations(rulebook):
            drift = _make(
                metadata_root,
                _edited(rulebook, change),
                f"drift_{name}",
                "drift",
                source.records,
                source.device_factors,
                real_data=source.real_data,
                description=f"{description} Input: {source.name}.",
                mutation=name,
            )
            if (drift.expected_rows, drift.expected_stats) == (
                source.expected_rows,
                source.expected_stats,
            ):
                raise ValueError(
                    f"mutation {name} does not change the engine's output on {source.case_id}"
                )
            cases.append(drift)
    return cases


def save_cases(cases: list[Case], directory: Path, manifest: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*/*.json"):
        stale.unlink()
    for case in cases:
        path = directory / case.vendor / f"{case.split}__{case.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(case)), encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def load_cases(directory: Path) -> list[Case]:
    paths = sorted(directory.glob("*/*.json"))
    if not paths:
        raise FileNotFoundError(
            f"no cases in {directory}; run `python harness.py build-cases` first"
        )
    return [Case(**json.loads(path.read_text(encoding="utf-8"))) for path in paths]
