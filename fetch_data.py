"""
Fetch rows through the "Datasphere_Joule" BTP Destination service.

Runs the SAME way in two environments:

  * Local          -> destination-service binding is read from .env
  * Cloud Foundry  -> destination-service binding is read from VCAP_SERVICES
                      (injected automatically once the app is bound with
                       `cf bind-service <app> <destination-instance>`)

Flow ("going through the destination", not calling the target directly):

  1. Get the destination-service instance's own OAuth client
     (clientid / clientsecret / xsuaa url / api uri).
  2. Fetch a client_credentials token for the destination service itself.
  3. GET /destination-configuration/v1/destinations/<name> to resolve the
     destination. The response carries destinationConfiguration (target URL,
     auth type, ...) and, for OAuth destinations, authTokens -- a bearer
     token the destination service already fetched for the target.
  4. Call the resolved target URL (+ DATA_PATH / DATA_QUERY) with that auth
     and print the rows.

Only dependency: requests.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Optional

import requests

TIMEOUT = 30


# --------------------------------------------------------------------------- #
# config loading
# --------------------------------------------------------------------------- #
def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no dependency). Does not override real env vars."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _get_destination_service_binding() -> dict:
    """
    Return {clientid, clientsecret, token_url, api_uri} for the bound
    destination-service instance.

    Prefers VCAP_SERVICES (Cloud Foundry). Falls back to DEST_* env vars
    (local .env).
    """
    vcap_raw = os.environ.get("VCAP_SERVICES")
    if vcap_raw:
        vcap = json.loads(vcap_raw)
        for entry in vcap.get("destination", []):
            c = entry["credentials"]
            return {
                "clientid": c["clientid"],
                "clientsecret": c["clientsecret"],
                "token_url": c["url"].rstrip("/") + "/oauth/token",
                "api_uri": c["uri"].rstrip("/"),
            }
        raise RuntimeError(
            "VCAP_SERVICES has no 'destination' entry -- bind this app to a "
            "destination-service instance and restage."
        )

    missing = [
        v
        for v in (
            "DEST_SVC_CLIENT_ID",
            "DEST_SVC_CLIENT_SECRET",
            "DEST_SVC_TOKEN_URL",
            "DEST_SVC_API_URL",
        )
        if not os.environ.get(v)
    ]
    if missing:
        raise RuntimeError(
            "No destination-service binding (VCAP_SERVICES absent and these "
            ".env vars missing: " + ", ".join(missing) + ")"
        )
    return {
        "clientid": os.environ["DEST_SVC_CLIENT_ID"],
        "clientsecret": os.environ["DEST_SVC_CLIENT_SECRET"],
        "token_url": os.environ["DEST_SVC_TOKEN_URL"],
        "api_uri": os.environ["DEST_SVC_API_URL"].rstrip("/"),
    }


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #
def get_service_token(binding: dict) -> str:
    resp = requests.post(
        binding["token_url"],
        data={"grant_type": "client_credentials"},
        auth=(binding["clientid"], binding["clientsecret"]),
        headers={"Accept": "application/json"},
        timeout=TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(
            f"XSUAA token request failed ({resp.status_code}): {resp.text[:400]}"
        )
    return resp.json()["access_token"]


def resolve_destination(binding: dict, svc_token: str, name: str) -> dict:
    url = f"{binding['api_uri']}/destination-configuration/v1/destinations/{name}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {svc_token}", "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Destination lookup for '{name}' failed ({resp.status_code}): "
            f"{resp.text[:400]}"
        )
    return resp.json()


def _target_auth_header(resolved: dict) -> Optional[str]:
    """Build the Authorization header value for the target call."""
    config = resolved.get("destinationConfiguration", {})
    tokens = resolved.get("authTokens") or []

    if tokens:
        tok = tokens[0]
        if tok.get("error"):
            raise RuntimeError(f"Destination auth token error: {tok['error']}")
        if tok.get("http_header", {}).get("value"):
            return tok["http_header"]["value"]
        if tok.get("value"):
            return f"{tok.get('type', 'Bearer')} {tok['value']}"

    # No pre-fetched token -> BasicAuthentication or explicit override.
    user = os.environ.get("DEST_BASIC_USER") or config.get("User")
    pwd = os.environ.get("DEST_BASIC_PASSWORD") or config.get("Password")
    if user and pwd:
        raw = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        return f"Basic {raw}"

    return None


def direct_resolve(name: str) -> dict:
    """
    Bypass the Destination *service* and do locally exactly what an
    OAuth2ClientCredentials destination does: client_credentials against the
    token service, then hand back a synthetic 'resolved destination' so the
    rest of the flow is identical.

    Needs DEST_CLIENT_ID / DEST_CLIENT_SECRET / DEST_TOKEN_URL and TARGET_URL
    (the real data host -- the Datasphere_Joule destination's own URL points at
    the auth host, which serves no data).
    """
    missing = [
        v
        for v in ("DEST_CLIENT_ID", "DEST_CLIENT_SECRET", "DEST_TOKEN_URL", "TARGET_URL")
        if not os.environ.get(v)
    ]
    if missing:
        raise RuntimeError("direct mode needs these .env vars: " + ", ".join(missing))

    resp = requests.post(
        os.environ["DEST_TOKEN_URL"],
        data={"grant_type": "client_credentials"},
        auth=(os.environ["DEST_CLIENT_ID"], os.environ["DEST_CLIENT_SECRET"]),
        headers={"Accept": "application/json"},
        timeout=TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(
            f"token service request failed ({resp.status_code}): {resp.text[:400]}"
        )
    token = resp.json()["access_token"]
    return {
        "destinationConfiguration": {
            "Name": name,
            "URL": os.environ["TARGET_URL"],
            "Authentication": "OAuth2ClientCredentials",
        },
        "authTokens": [{"type": "Bearer", "value": token}],
    }


def fetch_rows(resolved: dict, path: str, query: str) -> requests.Response:
    config = resolved["destinationConfiguration"]
    base = config["URL"].rstrip("/")
    url = f"{base}/{path.lstrip('/')}" if path else base
    if query:
        url = f"{url}?{query.lstrip('?')}"

    headers = {"Accept": "application/json"}
    auth = _target_auth_header(resolved)
    if auth:
        headers["Authorization"] = auth

    return requests.get(url, headers=headers, timeout=TIMEOUT)


# --------------------------------------------------------------------------- #
# reusable entry point (used by both the CLI below and app.py)
# --------------------------------------------------------------------------- #
def run_fetch(
    name: Optional[str] = None,
    data_path: Optional[str] = None,
    data_query: Optional[str] = None,
) -> dict:
    """
    Resolve `name` through the destination service (or direct OAuth fallback)
    and GET data_path?data_query from the resolved target.

    Any argument left as None falls back to the matching env var
    (DESTINATION_NAME / DATA_PATH / DATA_QUERY), same defaults the CLI uses.

    Returns a dict describing what happened -- never prints, never raises for
    an HTTP-level failure (that's reported via ok/status_code so callers, e.g.
    a FastAPI route, can turn it into the right response).  Raises RuntimeError
    for setup failures (bad/missing binding, bad destination credentials, ...).
    """
    name = name or os.environ.get("DESTINATION_NAME", "Datasphere_Joule")
    data_path = os.environ.get("DATA_PATH", "") if data_path is None else data_path
    data_query = os.environ.get("DATA_QUERY", "") if data_query is None else data_query

    # mode: "service" -> go through the Destination service REST API
    #       "direct"  -> replicate the destination's OAuth locally (no binding)
    #       "auto"    -> service if a binding is available, else direct
    mode = os.environ.get("FETCH_MODE", "auto").lower()

    if mode in ("auto", "service"):
        try:
            binding = _get_destination_service_binding()
        except RuntimeError:
            if mode == "service":
                raise
            binding = None
    else:
        binding = None

    if binding is not None:
        fetch_mode = "service"
        svc_token = get_service_token(binding)
        resolved = resolve_destination(binding, svc_token, name)
    else:
        fetch_mode = "direct"
        resolved = direct_resolve(name)

    cfg = resolved.get("destinationConfiguration", {})
    resp = fetch_rows(resolved, data_path, data_query)

    body_json = None
    body_text = None
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        try:
            body_json = resp.json()
        except ValueError:
            body_text = resp.text
    else:
        body_text = resp.text

    return {
        "fetch_mode": fetch_mode,
        "destination_name": name,
        "target_url": cfg.get("URL"),
        "auth_type": cfg.get("Authentication"),
        "prefetched_token": bool(resolved.get("authTokens")),
        "request_url": resp.url,
        "status_code": resp.status_code,
        "ok": resp.ok,
        "data": body_json,
        "text": body_text,
    }


# --------------------------------------------------------------------------- #
# CLI (unchanged diagnostic behaviour, now built on run_fetch)
# --------------------------------------------------------------------------- #
def main() -> int:
    _load_dotenv()

    result = run_fetch()

    print(f"[1/4] fetch mode              : {result['fetch_mode']}")
    print(f"[2/4] destination '{result['destination_name']}'    : resolved")
    print(f"       target URL            : {result['target_url']}")
    print(f"       auth type             : {result['auth_type']}")
    print(f"       pre-fetched token     : {result['prefetched_token']}")
    print(f"[3/4] GET {result['request_url']}")
    print(f"       http status           : {result['status_code']}")

    if result["data"] is not None:
        print("       response (json):")
        print(json.dumps(result["data"], indent=2)[:2000])
    elif result["text"] is not None:
        print("       response (text):")
        print(result["text"][:2000])

    print()
    ok = result["ok"]
    print("RESULT:", "CAN fetch data ✓" if ok else "CANNOT fetch data ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - top-level diagnostic
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
