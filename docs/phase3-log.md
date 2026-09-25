# Phase-3 log: canonical -> Delta / Unity Catalog

Working log for the platform integration (supporting infrastructure, not the thesis
contribution). Same discipline as the ProCem onboarding diary: verified facts, measured
effort, decisions recorded once.

## 2026-07-14, Step 0: access + handshake (closed)

All six handshake checks PASS (`scripts/phase3/handshake.py`):
configuration, Spark Connect session (client 4.1.1 <-> server 4.1.1), catalog reachable +
token accepted, `CREATE SCHEMA secha.canonical`, Delta round-trip
(create/insert/select/drop, catalog-managed commit path, NFS warehouse), staging path
visible to workers.

What it took (each found by a failing check, then fixed):

1. `secha` staging directory: `/net/nfs/data/secha` is `sparky`-owned; a stray root-owned
   dir from a sudo attempt had to be removed by plain `sparky` (parent-write rule).
   Rules learned: NEVER sudo for file ops under `/net/nfs` (root is squashed/powerless);
   `/net/nfs/data/secha/data/` holds the legacy transformer's Delta tables, never touch.
2. Unity Catalog service was down (manual tmux process, does not survive reboots);
   restarted; `secha` catalog created via UC CLI with the server admin token
   (storage root `file:///net/nfs/uc-warehouse/secha`).
3. Session-level registration of the `secha` catalog through Spark Connect WORKS
   (UCSingleCatalog per-session configs); no Connect-server change needed.
4. Managed tables REQUIRE `TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported')`
   on this UC (0.4, managed tables enabled); now declared in
   `secha-metadata/targets/canonical.yaml` `table_properties`.
5. Keycloak per-user token flow is broken platform-side;
   the UC admin token is the sanctioned stopgap, in `.env` only. When fixed: create the
   `secha` principal and grant catalog privileges (or make it owner of the `secha`
   catalog).

## 2026-07-14, Step 1: config (secha-metadata)

- `targets/canonical.yaml`: + `table_properties` (catalog-managed), + `staging`
  (`SECHA_STAGING_ROOT`), + `serving_schema: serving`. The catalog/schema/table names the
  handshake verified were already correct in config.
- New `serving/` layer (views as config-as-code): `pq_minute_wide.sql`, one SELECT over
  the `{canonical}` placeholder; the sink wraps it as CREATE OR REPLACE VIEW.
- Validator guards for serving views + 5 unit tests.

## 2026-07-14, Step 2: the Delta sink (built, offline-verified)

- `io/delta_sink.py` (~230 LOC): pure SQL builders at module top (DDL from
  `canonical_schema.yaml` + target `table_properties`; typed + deduped staging projection,
  latest `ingested_at` wins; explicit-column MERGE; serving-view wrapper resolving
  `{canonical}`), plus a thin `DeltaSink` session wrapper (Proven Connect pattern,
  pyspark imported lazily). Config-driven end to end: no table name, column, or property
  is hardcoded; unknown canonical types raise.
- CLI: `delta-load`, `delta-views`, `--sink delta` on both vendor commands; clean errors
  for missing env/extra. Settings + `.env.template` extended; `spark` extra pinned to
  `pyspark-client==4.1.1`.
- Tests: 11 offline builder tests pin every SQL statement; one env-gated integration test
  (real platform, throwaway table, dedupe assertion, MERGE-twice idempotency,
  self-cleaning) runs only when the Phase-3 env is present.

## 2026-07-15, Step 3: the live load (closed)

- Integration smoke test passed on the real platform (throwaway table; the generated DDL
  incl. the catalog-managed property, dedupe with latest-`ingested_at`-wins, MERGE-twice
  idempotency; self-cleaned). One test bug found and fixed on the way: the test's
  createDataFrame upload needed an explicit Arrow schema (an all-null harmonic_order
  column cannot be type-inferred). Test-only; the real path reads typed parquet.
- Both proven days staged to `/net/nfs/data/secha/canonical-staging/load-001` (219 MB,
  31 parquet files, scp as sparky) and MERGEd via `delta-load`.
- **Verified in Unity Catalog (`secha.canonical.measurement`):**
  - mx_electrix **36,000** + procem_kampusareena_pq **5,499,568** = 5,535,568 rows.
  - event_date partitions: 2025-08-15 (36,000), **2026-06-14 (358,496)**, 2026-06-15
    (5,141,072). The ProCem day splits across two UTC dates exactly as designed:
    canonical `event_date` follows `ts_utc`, while the source file was cut at
    Helsinki-local midnight. 358,496 + 5,141,072 = 5,499,568, reconciling exactly.

## 2026-07-15, Step 4 platform fact: the UC Spark connector has NO view ability

`delta-views` failed with `MISSING_CATALOG_ABILITY.VIEWS` (Spark analysis error: the
UCSingleCatalog 0.4.0 plugin does not implement view support; not an auth or SQL issue).
Adaptation, config-first: `targets/canonical.yaml` gained `serving_mode: table`, and the
sink materialises serving definitions as **Delta tables** (`CREATE OR REPLACE TABLE ... AS
SELECT`, with the platform table_properties) instead of views. The serving/*.sql bodies
are untouched: the {canonical} placeholder design made the definitions
materialisation-agnostic. Flip the config to `view` when the connector grows the ability.
Consequence to remember: table mode is a SNAPSHOT; re-run `delta-views` after each
`delta-load` to refresh serving data.

Second connector gap, same day: `CREATE OR REPLACE TABLE ... AS SELECT` failed too
(`UnsupportedOperationException: REPLACE TABLE AS SELECT (RTAS) is not supported`,
`stageCreateOrReplace`). Table mode therefore uses ONLY the primitives this platform has
already proven (handshake + Step-3 load): DROP TABLE IF EXISTS, CREATE TABLE with explicit
DDL (schema derived by ANALYSING the serving SELECT, no execution), INSERT INTO ... SELECT.
Non-atomic refresh (brief gap while the snapshot rebuilds); acceptable for Phase 3.
Upstream UC: connector lacks views, RTAS, and column comments.

## 2026-07-15, Step 4: serving + the platform convergence proof (closed)

- `delta-views` succeeded with the proven-primitives refresh:
  `view ready: secha.serving.pq_minute_wide`.
- **The convergence result, on TUNI production infrastructure:** one query over
  `secha.serving.pq_minute_wide` returns both vendors in identical wide columns
  (per-minute voltage/frequency/THD etc.). Sample: `mx_electrix:meter:21`,
  2025-08-15 00:00 UTC, `v_l1=237.2 V, f_hz=50.032, thd=1.04`.
- Per-vendor minute counts: mx_electrix **1,440** (complete day);
  procem_kampusareena_pq **1,349** of a possible 1,441. The ratio 1349/1441 = 93.6%
  matches the day's record completeness exactly (14,476,804 / 15,465,600 = 93.6%),
  i.e. the EVCharging gaps that day were whole-minute dropouts; the serving layer
  reflects source reality faithfully.
- Display note: Spark Connect renders TIMESTAMP in the session timezone (Helsinki),
  so `minute_utc` prints +3 h in August; stored values are correct UTC instants. For
  thesis exports, set `spark.sql.session.timeZone=UTC` in the session or format
  explicitly.

## Phase 3: CLOSED

End state: `secha` catalog on Unity Catalog holds `canonical.measurement`
(5,535,568 rows, two vendors, three event_date partitions incl. the Helsinki-boundary
split) and `serving.pq_minute_wide` (2,789 device-minutes, wide, quality-filtered),
all written by the config-driven sink over Spark Connect. Platform facts learned and
recorded: catalog-managed table property required; session-level catalog registration
works; connector lacks views + RTAS + column comments; root is powerless on NFS;
UC service is a manual tmux process; Keycloak per-user tokens broken (admin-token
stopgap in use).

## 2026-07-15, final proofs (recorded verbatim)

- **Platform-level idempotency:** second `delta-load` of the identical staging path
  reported `table 5535568 -> 5535568 rows`. The full chain is now idempotent at every
  level: content-hash landing skip, run-scoped parquet replace, and Delta MERGE.
- **The convergence exhibit** (one query on `secha.serving.pq_minute_wide`, two sample
  minutes per vendor; timestamps rendered in the session timezone, Helsinki):

  | source_vendor | device_id | minute (local) | v_l1 | f_hz | thd |
  |---|---|---|---|---|---|
  | mx_electrix | mx_electrix:meter:21 | 2025-08-15 03:00 | 237.20 | 50.032 | 1.04 |
  | mx_electrix | mx_electrix:meter:21 | 2025-08-15 03:01 | 237.29 | 49.995 | 1.03 |
  | procem_kampusareena_pq | procem:kampusareena:evcharging | 2026-06-15 00:00 | 234.59 | 49.999 | 0.99 |
  | procem_kampusareena_pq | procem:kampusareena:evcharging | 2026-06-15 00:01 | 234.28 | 49.989 | 1.00 |

  Two EV chargers (an ABC fuel station in Viinikka; the Kampusareena campus station),
  two vendors, wide-JSON-API vs long-file-dump pipelines, one query, one shape. Detail:
  ProCem's first serving minute is exactly local midnight, matching the source file's
  Helsinki-local day boundary.

## 2026-07-16, downstream feedback -> quantity dimension table

Downstream user confirmed he can query columns via SQL, but column descriptions do not
surface in the UC catalog UI, and even when comments are registered in UC, Spark SQL cannot
read them (two documented UC 0.4 limitations: empty column metadata on Spark-created tables;
comments not round-tripping Spark<->UC because Delta's log is the source of truth). Offered
two options: the legacy Scala transformer's REST-payload comment fix, or "metadata as a table
to query".

Decision: the second option, and it is the architecturally correct one for a LONG fact. In a
wide table each column is a measurement, so meaning must live in column comments (hence the
legacy fix). In our long fact the columns are structural and the meaning lives in ROWS, i.e.
in a DIMENSION. So we publish `secha.canonical.quantity` from `quantity_vocabulary.yaml`
(quantity, default_unit, standard_ref, description); user JOINs the fact to it. This is
config-driven, richer than a comment (carries standards refs), immune to the comment bug
(row values are always queryable), and completes the star schema (fact + dimensions). We did
NOT adopt the REST workaround: it belongs to the legacy transformer's manual UC registration,
and UC 0.5 fixes the underlying bug upstream. Report: recommend the UC 0.5 upgrade.

Implementation: `reference_dimensions` in `targets/canonical.yaml`; sink builders
(`dimension_rows`, `build_dimension_ddl` with real column comments, `build_dimension_insert`
with quote-escaping), all offline-tested; validator guard in secha-metadata (dangling
attribute -> all-null column, rejected). `delta-views` publishes dimensions then serving views.

## 2026-09-24, Kempower into Unity Catalog (third vendor, first entity dimension)

Code, all generic: the sink's builders take an optional entity. `entity_target()` resolves the
fact table by default, or any entity declared under `dimensions:` in `targets/canonical.yaml`
(its table, merge key and columns come from the config and the canonical schema), so
`charging_session` loads with the same DDL, dedupe and MERGE as the fact, on `session_id`.
The fact table's generated SQL is byte-identical to before. CLI: `delta-load --entity`.
Kempower's canonical output is 358,967,830 rows in 99 parts (14 GB) plus 396,848 sessions;
`scripts/phase3/load_kempower.sh` stages and loads it part by part, then the same parts'
sessions.

Platform facts found on the way (the platform changed since July, or these were not visible
then):

- The Connect server now runs **Spark 4.2.0** (the July handshake saw 4.1.1). The
  `pyspark-client==4.1.1` pin still works for everything the sink sends; move the pin to the
  server's version when convenient.
- The nodes are SunOS 5.11 (illumos zones). Six executors, three per worker, each 8 cores and
  4 GB heap plus 2 GB overhead. Workers spill and shuffle to `SPARK_LOCAL_DIRS=/tmp/spark`, and
  `/tmp` is a **32 GB tmpfs backed by swap**, while the zone's own disk has about 500 GB free.
  On illumos, process memory reserves the same swap, so a worker's room for shuffle files is
  what its three executors leave of those 32 GB (an inference from the numbers, not measured).
- Git Bash rewrites `/net/nfs/...` arguments into Windows paths (Spark then reports
  `Wrong FS: file://C:/Program Files/Git/net/nfs/...`); `MSYS_NO_PATHCONV=1` stops it.
- The Spark UI's REST API on the Connect host (`http://130.230.115.138:4040/api/v1`) answers
  over the VPN: job and stage failure reasons, executor disk use, persisted RDDs. It is
  read-only and the right place to diagnose a failed load.

**Pilot, part 0 (`load-002`), 13:05 to 13:08 UTC:** `secha.canonical.measurement`
5,535,568 -> 9,215,578 rows (+3,680,010) in 50 s; `secha.canonical.charging_session` created,
0 -> 3,593 rows in 17 s. Checked in Unity Catalog: Kempower's `event_date` is a real NULL (the
Hive default partition reads as null, not as a string); the other vendors' partitions are
unchanged (36,000; 358,496; 5,141,072); each of the five quantities has 736,002 rows, with
the declared phase and aggregation; 3 rows are suspect (the part's negative voltages);
`ts_utc` is null and `session_id` set on every row; 3,680,010 distinct `measurement_id`s;
every row joins `charging_session`. A second run of both loads changed nothing
(9,215,578 -> 9,215,578; 3,593 -> 3,593).

**The 10-part MERGE failed, and its failure blocked the next ones.** Parts 1 to 10 (77 files,
1.4 GB, about 37M rows) were staged as one directory. Because the source is not a Delta
table, Delta's MERGE first copies it to executor disk ("materialize source"). At 13:12 UTC
that copy filled a worker's `/tmp` (`No space left on device`, 130.230.115.141). The first
run's output had been cut short, so the MERGE was run a second time at 13:14 to read the
error; it failed the same way on 130.230.115.139. The table stayed at 9,215,578 rows (a Delta
commit is atomic). The second run left its partial copy behind on the executors: RDD 3922,
disk only, 11 of 24 partitions, 8.0 GB, of which 7.3 GB sit on the two executors of .141.
With that resident, every later job that writes shuffle files on .141 failed, whatever its
size: part 10 alone (3.7M rows) failed at the first row count of its staged files (13:19),
and a read-only `count(*)` on the table failed at 13:20.

Lessons: read a failed job's error from the REST API above; never run a heavy failing job
again just to see its message, because the re-run is what left the 8 GB. Size a MERGE against
the workers' scratch space, not against executor memory: 11 of 24 partitions took 8.0 GB,
so the whole 37M-row copy was heading for about 17 GB. One part per MERGE copies about
1.7 GB, which the pilot loaded without trouble.

**Recovery.** Spark's context cleaner runs a periodic garbage collection every 30 minutes (the
default; this application started 2026-09-14 17:12 UTC, so the ticks fall near :12 and :42).
At 13:42:03 it released RDD 3922, and every executor's disk use went to 0.00 GB. Nothing
was retried before then. The failed `count(*)` then passed: the table was still at
9,215,578 rows, version 4 (the pilot's second run), with 3,593 sessions.

**The subset (the decision after the failure):** the full 359M rows stay in local canonical
Parquet; every tenth part (0, 10, ..., 90) goes to Unity Catalog, one part per MERGE. From
13:43 to 13:55 UTC, parts 10 to 90 took 23 to 36 s each to stage and 44 to 57 s to load.
Each MERGE staged exactly its part's local row count (read from the Parquet footers), found
no duplicate on the key, and grew the table by exactly that count: 9,215,578 -> 41,884,113.
After every part the executors held 0.00 GB. The sessions of parts 10 to 90 (70 files,
2.8 MB) went in with one MERGE, `delta-load --entity charging_session`, in 18 s:
3,593 -> 40,175.

**Verified in Unity Catalog (read-only), each against counts taken from the local Parquet:**

- Rows by vendor: kempower 36,348,545 (the subset, 10.1% of the 358,967,830); mx_electrix
  36,000 and procem_kampusareena_pq 5,499,568, unchanged since July. Partitions: 2025-08-15
  36,000; 2026-06-14 358,496; 2026-06-15 5,141,072; null 36,348,545.
- Each of the five Kempower quantities has 7,269,709 rows, with the declared phase and
  aggregation (`dc` and `average` for power, current and voltage; `none` and
  `instantaneous` for state of charge and temperature).
- 55 rows are suspect, all voltage, as in the local files.
- On every Kempower row `ts_utc` and `event_date` are null and `session_id` is set.
- `charging_session`: 40,175 rows, 40,175 distinct, equal to the local distinct sessions of
  these ten parts (no session spans two of them). No measurement row names a session missing
  from the dimension.
- Uniqueness of `measurement_id` follows from the counts: each MERGE inserted every staged
  row, so none matched a row already in the table. A table-wide `count(DISTINCT)` would have
  meant a large shuffle on the same scarce scratch space, for no extra evidence.

**Serving refreshed.** Before `delta-views`, the quantity dimension lacked `state_of_charge`
and `temperature`, so 14,539,418 fact rows (two quantities x 7,269,709) found no row to JOIN.
After it (26 s): 20 quantities, 0 unjoined fact rows. `pq_minute_wide` is unchanged, 1,440
minutes for mx_electrix and 1,349 for procem, because its SELECT keeps only rows with clock
time and Kempower's rows have none.

Staging kept on the NFS: `load-002` (the pilot) and `load-003` (parts 10 to 90 and their
sessions, 1.3 GB), exactly what the table holds. `load-003/measurement-01`, the never-loaded
10-part directory, was removed on 2026-09-25 after all 77 of its files hashed identical
(SHA-256) to the local canonical Parquet.

For the platform owner: move the workers' `SPARK_LOCAL_DIRS` from the 32 GB swap-backed
`/tmp` to the zone disk, which would lift the one-part-per-MERGE limit; and a failed MERGE can
leave its source copy on the executors until the next periodic cleanup, up to 30 minutes.

## 2026-09-25, pre-commit review of the Kempower load

The sink now fails fast on a dimension whose `table` names no canonical entity or whose merge
key is not among its columns, and an entity without `ingested_at` keeps the same copy per key
on every run (ordered by its other staged columns). The fact table's SQL is byte-identical to
the load above. `load_kempower.sh` now checks every part against the local files before
staging, confirms each staged file by name, and MERGEs the same parts' sessions after their
measurements, which on 2026-09-24 was a separate manual step.

One slip during that review, recorded because it touched this staging area: a dry run of the
script, meant to use stand-in `ssh` and `scp`, reached the real host, because the stand-ins'
Windows path (`C:/...`) split `PATH` at its colon. It wrote 12 two-byte files into `load-003`
(a `part-00008` directory, and dummies over three measurement and three session files of part
10). No MERGE ran (the stand-in CLI did; the Spark REST API shows no job that day). Within the
hour the six overwritten files were restored from the local originals and the six new ones
removed, and all 140 staged files then hashed identical to local. The corrected dry run put
the stand-ins on `PATH` in POSIX form and refused to start unless `ssh` and `scp` resolved to
them; it passed, and a run in which a staged file went missing stopped before its MERGE.
