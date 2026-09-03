"""
FastAPI wrapper around fetch_data.run_fetch(), plus the agent (agent.py)
mounted as an A2A protocol server, for use as a Joule pro-code agent tool.

Endpoints:
  GET  /health  -> liveness check, no destination/LLM call.
  GET  /fetch   -> resolves the destination and returns the target's rows.
  /a2a/*        -> the agent (agent.py), wrapped as an A2A protocol server
                    (see a2a_server.py) -- mounted only if a2a-sdk imports
                    cleanly, so a broken/mismatched a2a-sdk version doesn't
                    take down /fetch. Send questions via A2A's SendMessage
                    JSON-RPC method, not a plain REST endpoint.

Run locally:    uvicorn app:app --reload
Run on CF:      uvicorn app:app --host 0.0.0.0 --port $PORT   (see manifest.yml)

Optional API-key protection: set the API_KEY env var and callers must send a
matching `X-API-Key` header. Unset (the default) means /fetch is open --
fine for a quick POC, but set API_KEY before wider exposure.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query

import fetch_data

fetch_data._load_dotenv()  # no-op on Cloud Foundry; picks up local .env

logger = logging.getLogger(__name__)


class _StripInboundTaskId:
    """ASGI middleware: drop any client-supplied taskId from A2A requests.

    Joule threads the previous turn's taskId back into every agent-request.
    This a2a-sdk rejects a re-sent taskId once that task is terminal
    ("Task ... is in terminal state: 3"), so every Joule turn after the
    first would fail. The agent keeps no per-conversation state, so each
    call is independent anyway -- strip params.taskId and
    params.message.taskId and let the server mint a fresh task. contextId
    is left intact for task grouping.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or not scope.get("path", "").startswith("/a2a")
        ):
            await self.app(scope, receive, send)
            return

        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break

        try:
            payload = json.loads(body)
            params = payload.get("params")
            if isinstance(params, dict):
                changed = params.pop("taskId", None) is not None
                message_obj = params.get("message")
                if isinstance(message_obj, dict):
                    changed = message_obj.pop("taskId", None) is not None or changed
                if changed:
                    body = json.dumps(payload).encode("utf-8")
        except (ValueError, AttributeError):
            pass  # not JSON we recognise -- forward unchanged

        consumed = False

        async def _receive():
            nonlocal consumed
            if consumed:
                return {"type": "http.disconnect"}
            consumed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, _receive, send)

app = FastAPI(
    title="Datasphere Fetch",
    description=(
        "Fetches rows from SAP Datasphere through the BTP Destination "
        "service, and answers questions about them, for use as a Joule "
        "pro-code agent tool."
    ),
    version="1.0.0",
)

try:
    import a2a_server

    _a2a_app = a2a_server.build_a2a_app(a2a_server.PUBLIC_BASE_URL)
    # Answer a bare POST /a2a (no trailing slash) directly. Registered
    # BEFORE the mount so Starlette matches it first -- otherwise the
    # Mount catches /a2a and 307-redirects to /a2a/, which Joule's POST
    # client doesn't follow.
    app.router.routes.append(a2a_server.bare_rpc_route("/a2a"))
    app.mount("/a2a", _a2a_app)
    app.add_middleware(_StripInboundTaskId)
except Exception:  # noqa: BLE001 - keep /fetch and /ask alive even if A2A wiring breaks
    logger.exception("A2A mount failed -- /a2a will be unavailable, other endpoints still work")


def _check_api_key(x_api_key: Optional[str]) -> None:
    expected = os.environ.get("API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/fetch")
def fetch(
    data_path: Optional[str] = Query(
        None, description="OData path appended to the destination's target URL. Defaults to the DATA_PATH env var."
    ),
    data_query: Optional[str] = Query(
        None, description="OData query string, e.g. '$top=10'. Defaults to the DATA_QUERY env var."
    ),
    destination_name: Optional[str] = Query(
        None, description="Destination to resolve. Defaults to the DESTINATION_NAME env var."
    ),
    x_api_key: Optional[str] = Header(None),
) -> dict:
    _check_api_key(x_api_key)

    try:
        result = fetch_data.run_fetch(
            name=destination_name, data_path=data_path, data_query=data_query
        )
    except Exception as exc:  # noqa: BLE001 - surface setup/auth failures as 502s
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not result["ok"]:
        raise HTTPException(
            status_code=502,
            detail={
                "status_code": result["status_code"],
                "target_url": result["target_url"],
                "body": result["data"] if result["data"] is not None else result["text"],
            },
        )

    return {
        "destination_name": result["destination_name"],
        "target_url": result["target_url"],
        "request_url": result["request_url"],
        "status_code": result["status_code"],
        "data": result["data"] if result["data"] is not None else result["text"],
    }
