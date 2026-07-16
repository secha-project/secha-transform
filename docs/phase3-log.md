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
