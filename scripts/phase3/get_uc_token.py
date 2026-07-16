"""Fetch a Unity Catalog access token via Keycloak (the PROPER per-user flow).

Env-driven rewrite of the platform's `get_unity_token.py`: same two-step exchange
(Keycloak password grant -> ID token -> Unity Control token exchange) but with all
credentials from the environment or a local `.env`, never hardcoded.

NOTE (2026-07): the platform notes report this Keycloak flow as currently broken
("Keycloak refuses the required HTTPS queries";). Until it is
fixed, use the admin token from the UC server instead (README.md section C). This script
exists so the switch back to per-user tokens is a .env change, not a code change.

Usage:
    python get_uc_token.py            # prints the UC token to stdout
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import requests

TIMEOUT_S = 15


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        print(f"missing environment variable: {name} (see .env.template)", file=sys.stderr)
        sys.exit(1)
    return value


def keycloak_id_token() -> str:
    host = _env("SECHA_KEYCLOAK_HOST")
    realm = _env("SECHA_KEYCLOAK_REALM")
    client_id = _env("SECHA_KEYCLOAK_CLIENT_ID")
    client_secret = _env("SECHA_KEYCLOAK_CLIENT_SECRET")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = requests.post(
        url=f"{host}/realms/{realm}/protocol/openid-connect/token",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        data={
            "grant_type": "password",
            "username": _env("SECHA_KEYCLOAK_USERNAME"),
            "password": _env("SECHA_KEYCLOAK_PASSWORD"),
            "scope": "openid",
        },
        timeout=TIMEOUT_S,
    )
    if not response.ok:
        raise RuntimeError(f"Keycloak token error: HTTP {response.status_code} {response.text}")
    id_token = response.json().get("id_token")
    if not id_token:
        raise RuntimeError("Keycloak response had no id_token")
    return str(id_token)


def exchange_for_uc_token() -> str:
    catalog_url = _env("SECHA_CATALOG_URL").rstrip("/")
    response = requests.post(
        url=f"{catalog_url}/api/1.0/unity-control/auth/tokens",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": keycloak_id_token(),
            "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        },
        timeout=TIMEOUT_S,
    )
    if not response.ok:
        raise RuntimeError(f"UC token exchange error: HTTP {response.status_code} {response.text}")
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("UC response had no access_token")
    return str(token)


if __name__ == "__main__":
    _load_dotenv(Path(__file__).parent / ".env")
    print(exchange_for_uc_token())
