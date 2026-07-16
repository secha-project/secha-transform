"""Phase-3 Step-0 handshake: verify every assumption the Delta/UC sink will rest on.

Runs seven ordered checks against the TUNI data platform (VPN required), each with a clear
PASS/FAIL/SKIP verdict and, on failure, a diagnosis of the likely cause plus the exact fix.
Configuration comes from environment variables or a local `.env` file (never committed);
no secret is ever printed.

Checks:
  1. configuration is complete
  2. Spark Connect session opens (client/server version handshake)
  3. the Unity Catalog plugin registers for our catalog name at SESSION level
  4. the catalog exists and the token is accepted (SHOW SCHEMAS)
  5. we may CREATE SCHEMA (write permission)
  6. Delta round-trip: CREATE TABLE / INSERT / SELECT / DROP in the catalog
  7. the NFS staging path is visible to the Spark workers (optional marker file)

Usage:
    pip install -r requirements.txt
    cp .env.template .env   # fill in values; see README.md for where they come from
    python handshake.py
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pyspark is imported lazily so a missing install fails with guidance
    from pyspark.sql import SparkSession

REQUIRED_VARS = ("SECHA_SPARK_URL", "SECHA_CATALOG_URL", "SECHA_CATALOG_TOKEN")
CATALOG_DEFAULT = "secha"
SCHEMA = "canonical"
HANDSHAKE_TABLE = "_handshake"
MARKER_FILE = "_handshake_marker.txt"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str]] = []


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines; no dependency, no shell needed on Windows)."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _report(name: str, verdict: str, detail: str = "") -> None:
    _results.append((name, verdict, detail))
    print(f"[{verdict}] {name}" + (f": {detail}" if detail else ""))


def _die(name: str, diagnosis: str) -> None:
    _report(name, FAIL, "")
    print()
    print("Diagnosis and fix:")
    print(diagnosis)
    _summary()
    sys.exit(1)


def _summary() -> None:
    print()
    print("=" * 62)
    print("Handshake summary")
    for name, verdict, _ in _results:
        print(f"  [{verdict}] {name}")
    print("=" * 62)


def check_configuration() -> tuple[str, str, str, str, str | None]:
    missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        _die(
            "configuration",
            f"  Missing environment variables: {', '.join(missing)}.\n"
            "  Copy .env.template to .env and fill it in (see README.md).\n"
            "  While the Keycloak flow is broken, SECHA_CATALOG_TOKEN is the admin token\n"
            "  from the UC server (README.md section C).",
        )
    spark_url = os.environ["SECHA_SPARK_URL"]
    catalog_url = os.environ["SECHA_CATALOG_URL"]
    token = os.environ["SECHA_CATALOG_TOKEN"]
    catalog = os.environ.get("SECHA_CATALOG_NAME", CATALOG_DEFAULT)
    staging = os.environ.get("SECHA_STAGING_ROOT")
    _report(
        "configuration",
        PASS,
        f"spark={spark_url}, catalog={catalog} @ {catalog_url}, token set (not shown)",
    )
    return spark_url, catalog_url, token, catalog, staging


def check_session(spark_url: str, catalog_url: str, token: str, catalog: str) -> SparkSession:
    try:
        from pyspark.sql import SparkSession
    except ImportError:
        _die(
            "pyspark-client import",
            "  pip install -r requirements.txt (pyspark-client==4.1.1; the version is pinned\n"
            "  to the server's Spark 4.1.1, do not upgrade it independently).",
        )
    started = time.monotonic()
    try:
        spark = (
            SparkSession.builder.config(
                f"spark.sql.catalog.{catalog}", "io.unitycatalog.spark.UCSingleCatalog"
            )
            .config(f"spark.sql.catalog.{catalog}.uri", catalog_url)
            .config(f"spark.sql.catalog.{catalog}.token", token)
            .config("spark.sql.defaultCatalog", catalog)
            .remote(spark_url)
            .getOrCreate()
        )
        version = spark.version
    except Exception as error:
        _die(
            "spark connect session",
            f"  Could not open a session at {spark_url}.\n"
            "  1. Are you on TUNI VPN / eduVPN? (all platform ports require it)\n"
            "  2. Is the Connect server up? (Spark web UI: http://130.230.115.138:4040)\n"
            f"  Error: {error}",
        )
    elapsed = time.monotonic() - started
    _report("spark connect session", PASS, f"server Spark {version} in {elapsed:.1f}s")
    return spark


def check_catalog(spark: SparkSession, catalog: str) -> None:
    try:
        schemas = [row[0] for row in spark.sql(f"SHOW SCHEMAS IN {catalog}").collect()]
    except Exception as error:
        text = str(error)
        if (
            "Failed HTTP request" in text
            or "Connection refused" in text
            or "timed out" in text.lower()
        ):
            # the UC plugin loaded and made an HTTP call, so registration WORKED;
            # the UC API itself is unreachable from the Connect server
            _die(
                "unity catalog API reachable",
                "  The Connect server could not reach the Unity Catalog API (transport\n"
                "  failure, not an auth or catalog error). Most likely the UC process is\n"
                "  down: it runs in a manual tmux session and does not survive reboots.\n"
                "  1. From your laptop (VPN on), check:  http://130.230.115.140:3000\n"
                "  2. If down, restart it (README.md, troubleshooting section):\n"
                "       ssh sparky@130.230.115.140\n"
                "       cd /home/sparky/unitycatalog-0.4.0\n"
                "       tmux new -s catalog    # then inside: ./bin/start-uc-server\n"
                "       (detach with Ctrl+b then d)\n"
                f"  Error: {error}",
            )
        if "ClassNotFound" in text or "Cannot find catalog plugin" in text:
            _die(
                "catalog registration",
                "  The session-level catalog registration was rejected. Fallback:\n"
                "  add these two lines to the Connect server start command and restart it:\n"
                f"    --conf spark.sql.catalog.{catalog}=io.unitycatalog.spark.UCSingleCatalog\n"
                f"    --conf spark.sql.catalog.{catalog}.uri=<catalog url>\n"
                f"  Error: {error}",
            )
        if "401" in text or "403" in text or "Unauthorized" in text or "token" in text.lower():
            _die(
                "catalog auth",
                "  Unity Catalog rejected the token. While Keycloak is broken, use the admin\n"
                "  token from the UC server (README.md section C) in SECHA_CATALOG_TOKEN.\n"
                f"  Error: {error}",
            )
        _die(
            "catalog exists",
            f"  Could not list schemas in '{catalog}'. If the catalog does not exist yet,\n"
            "  create it on the UC server (README.md section C):\n"
            f'    bin/uc --auth_token "$(cat etc/conf/token.txt)" catalog create \\\n'
            f'        --name {catalog} --storage_root "file:///net/nfs/uc-warehouse/{catalog}"\n'
            f"  Error: {error}",
        )
    _report("catalog reachable + token accepted", PASS, f"schemas: {schemas or '(none yet)'}")


def check_schema(spark: SparkSession, catalog: str) -> None:
    try:
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{SCHEMA}")
    except Exception as error:
        _die(
            "create schema",
            f"  CREATE SCHEMA failed in catalog '{catalog}': the token lacks write rights\n"
            "  or the catalog storage root is misconfigured.\n"
            f"  Error: {error}",
        )
    _report("create schema", PASS, f"{catalog}.{SCHEMA} ready")


def check_delta_round_trip(spark: SparkSession, catalog: str) -> None:
    table = f"{catalog}.{SCHEMA}.{HANDSHAKE_TABLE}"
    try:
        spark.sql(f"DROP TABLE IF EXISTS {table}")
        # this UC (0.4, managed tables enabled) REQUIRES the catalog-managed Delta feature
        # on every managed table; the Phase-3 sink's generated DDL must include it too
        spark.sql(
            f"CREATE TABLE {table} (id INT, note STRING) USING DELTA "
            "TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported')"
        )
        spark.sql(f"INSERT INTO {table} VALUES (1, 'hello from secha-transform handshake')")
        count = spark.sql(f"SELECT count(*) FROM {table}").collect()[0][0]
        if count != 1:
            raise RuntimeError(f"expected 1 row after insert, found {count}")
    except Exception as error:
        _die(
            "delta round-trip",
            "  Creating/writing/reading a managed Delta table failed. This exercises the\n"
            "  UC connector AND the NFS storage root, so check both the token rights and\n"
            "  that /net/nfs is mounted on the Spark servers (servers.md: remount command).\n"
            f"  Error: {error}",
        )
    finally:
        # best-effort cleanup: never leave handshake junk behind
        with contextlib.suppress(Exception):
            spark.sql(f"DROP TABLE IF EXISTS {table}")
    _report("delta round-trip (create/insert/select/drop)", PASS, "managed table on NFS works")


def check_staging_visibility(spark: SparkSession, staging: str | None) -> None:
    name = "staging path visible to workers"
    if not staging:
        _report(name, SKIP, "SECHA_STAGING_ROOT not set")
        return
    marker = f"file://{staging.rstrip('/')}/{MARKER_FILE}"
    try:
        first = spark.read.text(marker).collect()
    except Exception as error:
        _report(
            name,
            SKIP,
            f"marker not readable ({marker}). Create it over SSH (README.md section B), "
            f"then re-run. Error: {type(error).__name__}",
        )
        return
    detail = first[0][0] if first else "(empty marker)"
    _report(name, PASS, detail)


def main() -> None:
    _load_dotenv(Path(__file__).parent / ".env")
    print("secha-transform Phase-3 handshake (VPN required)")
    print("-" * 62)
    spark_url, catalog_url, token, catalog, staging = check_configuration()
    spark = check_session(spark_url, catalog_url, token, catalog)
    try:
        check_catalog(spark, catalog)
        check_schema(spark, catalog)
        check_delta_round_trip(spark, catalog)
        check_staging_visibility(spark, staging)
    finally:
        spark.stop()
    _summary()
    if all(verdict != FAIL for _, verdict, _ in _results):
        print("All required checks passed. Phase-3 Step 2 (the Delta sink) is unblocked.")


if __name__ == "__main__":
    main()
