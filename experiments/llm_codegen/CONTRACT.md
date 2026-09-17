# The canonical transformation contract

This document is the specification a transformation program must satisfy to produce the
same canonical output as the `secha-transform` engine. It is written from the engine's
documented behaviour (its module and function docstrings, the canonical schema and the
target binding in `secha-metadata`), not from its source code, and it was frozen before any
model output was seen. Every generated program is scored against the engine's actual output
on the same input, so where this document and the engine ever disagree, the engine wins and
the disagreement is a defect in this document.

The key words MUST and MUST NOT are normative.

## 1. Input

The program reads one JSON object from standard input:

```json
{
  "records": [ { "...": "one raw source record" } ],
  "device_factors": { "21": { "uk": 1.0, "ik": 500.0 } }
}
```

- `records` is a list of raw records exactly as the landing zone delivers them after parsing.
  JSON sources keep their JSON types. Delimited text sources arrive with every value as a
  string, so numbers and epochs are strings and MUST be interpreted by the program.
- `device_factors` maps a meter identifier, as a string, to its scaling factors. It may be
  empty, and a meter may be absent from it.
- In interpreter mode the object also carries `rulebook` (see section 8).

## 2. Output

The program writes one JSON object to standard output:

```json
{ "rows": [ { "...": "one canonical row" } ], "stats": { "...": 0 } }
```

Each row MUST have exactly these fields:

| Field | Type | Meaning |
|---|---|---|
| `measurement_id` | string | deterministic identity hash, section 6 |
| `source_vendor` | string | the rulebook's vendor name |
| `source_dataset` | string | `mapping.source` |
| `device_id` | string | section 4.2 |
| `ts_utc` | string or null | section 4.3 |
| `quantity` | string | from the mapping |
| `phase` | string | from the mapping |
| `variant` | string | from the mapping, default `none` |
| `harmonic_order` | integer or null | from the mapping, default null |
| `value` | number | section 5 |
| `unit` | string | from the mapping |
| `aggregation` | string | section 4.5 |
| `interval_s` | integer or null | `source_schema.defaults.interval_s`, else null |
| `quality` | string | `ok` or `suspect`, section 4.6 |
| `source_row_id` | string or null | section 4.4 |
| `schema_version` | string | `mapping.target_schema_version` as a string, default `"1.0.0"` |

Row order is not significant. `stats` MUST have exactly these integer fields:
`records_in`, `records_rejected`, `records_unmapped`, `rows_emitted`, `rows_suspect`,
`rows_dropped`, `cells_null_skipped`.

## 3. Validation rules

`validation.rules` is a list processed in declaration order. There are three kinds.

- A **record rule** has `field` and neither `quantity` nor `entity`. It checks the raw
  record's value for that field.
- A **quantity rule** has `quantity`. It checks the transformed value of every candidate row
  of that quantity.
- An **entity rule** with `entity: measurement`, `field: value`, `type: not_null` is
  satisfied by construction, because null values are never emitted. It requires no action.

Check types:

- `not_null` fails when the value is missing or null.
- `range` passes when the value is missing or null. Otherwise it fails when the value, as a
  number, is below `min` or above `max`, where either bound may be absent.

Outcomes when a check fails are given in section 4.

## 4. Processing each record

Records are processed one at a time, in input order.

### 4.1 Counting and record rules

1. Increment `records_in`.
2. Evaluate record rules in order. On failure, `flag_suspect` marks the record suspect and
   evaluation continues. `reject_row` or `drop_row` discards the record: increment
   `records_rejected`, emit nothing for it, and move to the next record.

### 4.2 Device identifier

`device_id` is `source_schema.record.device_id_template` with each `{field}` placeholder
replaced by that field's value in the record, using Python `str.format` semantics. With no
template it is `"<vendor>:meter:{<meter_field>}"` when `record.meter_field` is declared, and
`"<vendor>:device"` otherwise.

### 4.3 Timestamp

The raw timestamp is the record's `record.timestamp_field` (default `timestamp`), interpreted
per `source_schema.format.datetime_format` (default `iso8601`):

- If the raw value is null, `ts_utc` is null.
- `iso8601`: take the value as a string. If it does not parse as an ISO-8601 datetime,
  `ts_utc` is null. If it parses and carries a UTC offset, `ts_utc` is the original string,
  unchanged. If it parses and is naive, `ts_utc` is the original string with `Z` appended.
- `epoch_ms`: the value is an integer number of milliseconds since the Unix epoch. Render it
  as `YYYY-MM-DDTHH:MM:SS.mmmZ` in UTC with exactly three fractional digits, using integer
  arithmetic so milliseconds are never rounded. If it is not an integer, `ts_utc` is null.
- `epoch_s`: integer seconds, rendered as `YYYY-MM-DDTHH:MM:SSZ` in UTC. If it is not an
  integer, `ts_utc` is null.

### 4.4 Row identifier and factors

`source_row_id` is the record's `record.row_id_field` (default `id`) converted to a string,
or null when that field is missing or null.

When `record.meter_field` is declared, the record's factors are
`device_factors[str(record[meter_field])]`, or none if that meter is absent. Without a meter
field the record has no factors.

### 4.5 Candidate rows

The source shape is `source_schema.shape`, default `wide`. The default aggregation is
`source_schema.defaults.aggregation`, or `instantaneous` when not declared.

**Long shape.** The record is one reading.

1. Look up `str(record[record.key_field])` among the `key` values of `mapping.rows`. If no
   entry matches, increment `records_unmapped` and emit nothing. The record is not rejected.
2. If the record's `record.value_field` is missing or null, increment `cells_null_skipped`
   and emit nothing.
3. Otherwise produce one candidate row from the entry: its `quantity`, `phase`, `unit`,
   `variant` (default `none`), `harmonic_order` (default null) and `aggregation` (the
   entry's own value if present, otherwise the default aggregation), with the value from
   section 5. If the transform yields no value, emit nothing and count nothing.

**Wide shape.** The record carries many fields.

1. For each entry of `mapping.columns`, in order: if the record's `src` field is missing or
   null, increment `cells_null_skipped` and continue. Otherwise compute the value per
   section 5. If the transform yields no value, continue without counting. Otherwise produce
   a candidate row with the entry's `quantity`, `phase`, `unit`, `variant` (default `none`),
   `harmonic_order` null, and the default aggregation.
2. Then for each entry of `mapping.generated`, for each `order` in its `order` list, for each
   `(index, phase)` pair of its `phase_map` in declared order: the source field is `pattern`
   with `{order}` replaced by the order and `{p}` replaced by the index. If that field is
   missing or null, increment `cells_null_skipped` and continue. Otherwise produce a candidate
   row with the entry's `quantity`, that `phase`, `variant` `none`, `harmonic_order` equal to
   the order as an integer, the entry's `unit`, the value as a float with no transform, and
   the default aggregation.

### 4.6 Quantity rules, quality and rejection

For each candidate row in production order, evaluate the quantity rules for its quantity, in
declaration order, against the row's value:

- `flag_suspect`: the row's quality becomes `suspect`; keep evaluating.
- `drop_row`: discard this row, increment `rows_dropped`, stop evaluating this row.
- `reject_row`: discard the whole record, including rows already accepted from it, increment
  `records_rejected`, and stop processing the record.

A row's quality is `suspect` if its record was flagged in 4.1 or any rule flagged the row,
and `ok` otherwise.

`rows_suspect` counts output rows whose quality is `suspect`. Rows of a rejected record never
count. `rows_emitted` is the number of output rows.

## 5. Values and transforms

The column's `transform` (default `none`) with its `args`:

- `none`: the value is `float(raw)`.
- `scale_by_factor`: `args.factor_field` names the factor. `uk` and `ik` come from the
  record's factors; `uk_ik` means the product `uk * ik` and requires both. When a needed
  factor is unavailable the transform yields no value. Otherwise the value is
  `float(raw) * factor`, where for `uk_ik` the factor is computed first.
- `parse_decimal`: replace `args.decimal_separator` (default `.`) with `.` and take the float.
- Any other transform name is an error and the program MUST fail loudly.

## 6. Identity hash

The identity fields, in this order, come from `target.measurement_id_from`:

`source_vendor, device_id, ts_utc, quantity, phase, variant, harmonic_order, aggregation, source_row_id`

Build the payload by joining `name=repr(value)` for each field with `|`, where `repr` is
Python's `repr` of the row's value (so strings are quoted and null is `None`). The
`measurement_id` is the first 32 characters of the lowercase hex SHA-256 digest of the payload
encoded as UTF-8.

## 7. Constraints

- Python 3.11 or later, standard library only. No third-party packages are installed.
- Read the input from standard input and write the output to standard output. Do not read or
  write files, open network connections, start processes or read environment variables.
- The time zone database is not available. Use `datetime.timezone.utc` for UTC.
- The output MUST be deterministic: the same input always gives the same output.

## 8. Interpreter mode

In interpreter mode the input also carries the rulebook, so the program must derive all
vendor behaviour from it rather than from constants written into the program:

```json
{
  "records": [],
  "device_factors": {},
  "rulebook": {
    "vendor": "mx_electrix",
    "source_schema": {},
    "mapping": {},
    "validation": {},
    "target": { "measurement_id_from": [] }
  }
}
```

The rulebook objects are the YAML files of the vendor directory parsed into JSON. The
program is evaluated on vendors other than the one whose rulebook it was shown, and on
rulebooks that have been edited after it was written.
