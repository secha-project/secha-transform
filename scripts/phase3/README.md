# Phase-3 Step 0: platform handshake runbook

Verifies every assumption the Delta/Unity-Catalog sink (Phase-3 Step 2) rests on, end to end,
in one connected session. Everything here is idempotent: safe to re-run at any point.

**Prerequisites:** TUNI VPN or eduVPN connected; your SSH key is on the platform servers;
the confidential `data-platform-documentation` repo at hand for credential values. **No secret from that repo
may ever be committed anywhere.**

The server map (from `servers.md`):

| Role | Address | Accounts |
|---|---|---|
| Spark Connect | `130.230.115.138` (spark-1) | `secha` (daily), `sparky` (sudo, no password) |
| Spark master | `130.230.115.142` (spark-4) | same |
| Spark workers | `130.230.115.139`, `130.230.115.141` | same |
| Unity Catalog | `130.230.115.140` | `secha`, `sparky` (sudo password in servers.md) |

## A. Connect the VPN

eduVPN or TUNI VPN. Every platform port requires it, including SSH to the `130.230.115.*` hosts.

## B. One-time: staging directory + visibility marker (Spark server)

Two facts learned the hard way (2026-07-07 session), baked into this sequence:

- **NFS root squash is active**: `sudo` has NO special powers under `/net/nfs`; root maps to an
  unprivileged identity there. Ownership fixes can only happen on the storage server.
- **`/net/nfs/data/secha` is NOT free**: it already holds the legacy `spark-data-transformer`
  Delta tables (`device_15/23/26/37/156`, written by Spark jobs, most likely as `sparky`).
  Never chown or write into the legacy tables; stage BESIDE them, not over them.

### B.1 Diagnose (read-only, safe)

```bash
ssh sparky@130.230.115.138
id                                        # note sparky's uid/gid
ls -la /net/nfs/data/ | head              # who owns what; is 'secha' dir sparky-owned?
ls -la /net/nfs/data/secha/ | head        # legacy tables + the accidental canonical-staging
ls -ld /net/nfs/data/secha/canonical-staging 2>/dev/null   # owner of the accidental dir, if it exists
```

### B.2 Create the staging dir as PLAIN sparky (no sudo)

Verified 2026-07-14: `/net/nfs/data/secha` is owned `sparky:staff` mode 755, so `sparky` (and
only `sparky`, without sudo) manages entries there. The legacy Delta tables live one level down
in `/net/nfs/data/secha/data/`; `canonical-staging` sits beside them, never inside.

If a stray root-owned `canonical-staging` exists (left by the first sudo attempt), remove it
first: deleting a directory needs write permission on the PARENT (sparky-owned), not ownership
of the directory itself, and the stray dir is empty, so plain `sparky` may remove it.

```bash
rmdir /net/nfs/data/secha/canonical-staging      # only if a stray root-owned one exists
mkdir /net/nfs/data/secha/canonical-staging      # plain sparky, NOT sudo -> sparky-owned
ls -ld /net/nfs/data/secha/canonical-staging     # verify owner is sparky
date -u +"secha handshake marker %Y-%m-%dT%H:%M:%SZ" > /net/nfs/data/secha/canonical-staging/_handshake_marker.txt
cat /net/nfs/data/secha/canonical-staging/_handshake_marker.txt
exit
```

If even the `rmdir` is denied: ask to fix it ON THE STORAGE SERVER
(`192.168.57.2:/tank/nfs-dsc`), since root on the Spark nodes is powerless under `/net/nfs`.

Notes: staging uploads (scp) go as `sparky`, the directory owner:
`scp <files> sparky@130.230.115.138:/net/nfs/data/secha/canonical-staging/`.
`/net/nfs` is mounted on all Spark and UC servers (NOT on secha-server), which is why staging
goes through a Spark node.

## C. One-time: create the `secha` catalog + fetch the token (UC server)

```bash
ssh sparky@130.230.115.140          # sudo password: servers.md, Unity Catalog section
cd /home/sparky/unitycatalog-0.4.0

# what exists today:
bin/uc --auth_token "$(cat etc/conf/token.txt)" catalog list

# create our dedicated catalog with its own storage root (same pattern used for 'unity'):
bin/uc --auth_token "$(cat etc/conf/token.txt)" catalog create \
    --name secha --storage_root "file:///net/nfs/uc-warehouse/secha"

# the admin token, needed on your laptop while the Keycloak flow is broken.
# Copy the OUTPUT into scripts/phase3/.env as SECHA_CATALOG_TOKEN. Do not store it anywhere else.
cat etc/conf/token.txt
exit
```

Keycloak note: the proper per-user token flow (`get_uc_token.py` here) is currently broken
platform-side ("Keycloak refuses the required HTTPS queries", per the platform notes.
The admin token is the sanctioned stopgap; switching back later is a `.env` change only.

## D. The handshake (laptop)

```bash
cd secha-transform/scripts/phase3
python -m venv .venv-phase3
.venv-phase3/Scripts/pip install -r requirements.txt      # Linux/macOS: .venv-phase3/bin/pip
cp .env.template .env                                     # fill in SECHA_CATALOG_TOKEN
.venv-phase3/Scripts/python handshake.py
```

What each check proves, in order:

| Check | Proves |
|---|---|
| spark connect session | VPN + gRPC endpoint + client 4.1.1 vs server 4.1.1 handshake |
| catalog registration | `UCSingleCatalog` accepts SESSION-level registration for a new name |
| catalog reachable | the `secha` catalog exists and the token is accepted |
| create schema | the token has write rights (`secha.canonical`) |
| delta round-trip | managed Delta table create/insert/select/drop works on the NFS warehouse |
| staging visibility | Spark workers can read `/net/nfs/data/secha/canonical-staging` |

Every check that fails prints its diagnosis and the exact fix. The one with a known fallback is
catalog registration: if Spark refuses session-level registration, ask to add two `--conf`
lines to the Connect server start command (the script prints them verbatim).

## Troubleshooting: Unity Catalog API unreachable ("Failed HTTP request after 3 attempts")

Seen 2026-07-14. The UC plugin registered fine; the Connect server simply could not reach the
UC API at `130.230.115.140:8080`. The UC process runs in a MANUAL tmux session (servers.md), so
it does not survive a reboot of its host. Check and restart:

```bash
# from the laptop (VPN on): does the web UI answer? http://130.230.115.140:3000
ssh sparky@130.230.115.140
tmux ls                                    # is there a 'catalog' session?
cd /home/sparky/unitycatalog-0.4.0
tmux new -s catalog                        # or: tmux attach -t catalog
./bin/start-uc-server                      # inside the tmux session
# detach with Ctrl+b then d, and verify from the laptop again, then re-run handshake.py
exit
```

If the UC web UI (:3000) works but the handshake still fails, isolate WHICH link is broken
(seen 2026-07-14: UI up, catalog visible, spark-1 still could not reach the API):

```bash
# A. laptop: is the API (:8080, not just the UI) reachable externally?
curl.exe -s -o NUL -w "%{http_code}`n" --connect-timeout 5 http://130.230.115.140:8080/api/2.1/unity-catalog/catalogs
# B. from spark-1 (the exact failing path):
ssh secha@130.230.115.138 "curl -s -o /dev/null -w '%{http_code}\n' --connect-timeout 5 http://130.230.115.140:8080/api/2.1/unity-catalog/catalogs"
# C. on the UC server: binding + duplicate instances
ssh sparky@130.230.115.140 "ss -tlnp | grep -E ':8080|:3000'; tmux ls"
```

Readings: A+B fail with binding `127.0.0.1:8080` = UC restarted localhost-only (fix the start
config). A+B fail with `*:8080` = host firewall (`ufw status`) or two UC instances fighting for
the port. A works, B fails = network filtering between spark-1 and the UC host.

## Platform rules learned via the handshake (bake into the Step-2 sink)

- **Managed Delta tables MUST declare the catalog-managed feature** (seen 2026-07-14):
  `CREATE TABLE … USING DELTA TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported')`.
  UC 0.4 with `server.managed-table.enabled=true` rejects managed-table creation without it.
  The Delta sink's generated DDL must always include this property on this platform.

## Step-3 runbook: stage + MERGE the two proven days (VPN throughout)

Pre-verified local input: `data/canonical` = 5,535,568 rows (mx_electrix 36,000 +
procem_kampusareena_pq 5,499,568), 219 MB, 31 parquet files. If it ever needs regenerating:
`rm data/canonical` then run both vendor commands (procem takes ~6 min).

```powershell
# 1. one-time: the spark extra into the MAIN venv + repo-root .env
.venv/Scripts/pip install -e ".[spark]"
Copy-Item .env.template .env      # then fill SECHA_CATALOG_TOKEN (same value as scripts/phase3/.env)

# 2. smoke test against the real platform (reads process env, not .env):
$env:SECHA_SPARK_URL = "sc://130.230.115.138:15772"
$env:SECHA_CATALOG_URL = "http://130.230.115.140:8080"
$env:SECHA_CATALOG_TOKEN = "<token>"
.venv/Scripts/python -m pytest tests/test_delta_integration.py -v     # expect: 1 passed

# 3. stage. IMPORTANT: the destination dir must NOT exist yet, or scp nests a second
#    'canonical' inside it. Pick a new load-NNN name per upload.
scp -r data/canonical sparky@130.230.115.138:/net/nfs/data/secha/canonical-staging/load-001
ssh sparky@130.230.115.138 "find /net/nfs/data/secha/canonical-staging/load-001 -name '*.parquet' | wc -l"
#   expect: 31

# 4. the load, TWICE (second run is the platform-level idempotency proof):
.venv/Scripts/secha-transform delta-load --staging /net/nfs/data/secha/canonical-staging/load-001
#   expect: table 0 -> 5535568 rows
.venv/Scripts/secha-transform delta-load --staging /net/nfs/data/secha/canonical-staging/load-001
#   expect: table 5535568 -> 5535568 rows
```

A VPN drop mid-MERGE is safe: the MERGE is idempotent, just re-run. `merged_rows` slightly
below `staged_rows` is fine (genuine same-instant duplicate readings deduped). Afterwards the
table is visible in the UC web UI (:3000, secha -> canonical -> measurement).

## Step-0 done criteria

All checks PASS (staging may be SKIP only if you have not created the marker yet). Then Step 1
(config: pin `targets/canonical.yaml` to the confirmed catalog, add the serving view) and Step 2
(the Delta sink) are unblocked, and this folder's `.env` already holds everything Step 3 needs.

**Outcome (2026-07-15): all Phase-3 steps completed and verified live.** The canonical table
(5,535,568 rows, both vendors) and the `pq_minute_wide` serving snapshot exist in Unity Catalog;
the second `delta-load` reported `5535568 -> 5535568` (idempotency). Full measured record:
`docs/phase3-log.md`. This folder remains the operational runbook for future loads.
