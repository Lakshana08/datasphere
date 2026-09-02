"""
LangChain tool-calling agent that answers natural-language questions using
data fetched from configured Datasphere paths (via fetch_data.run_fetch()).

Add more paths later by adding entries to DATA_SOURCES below -- each one
becomes a separately named dataset the agent's fetch_dataset tool can pull.
No other code needs to change when a new path is added.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from langchain.agents import create_agent
from langchain_core.tools import tool

import fetch_data

fetch_data._load_dotenv()  # no-op on Cloud Foundry; picks up local .env


# --------------------------------------------------------------------------- #
# SAP AI Core credentials
#
# The gen_ai_hub SDK reads AICORE_CLIENT_ID / AICORE_CLIENT_SECRET /
# AICORE_AUTH_URL / AICORE_BASE_URL / AICORE_RESOURCE_GROUP from the
# environment (or ~/.aicore/config.json -- not used here). On Cloud Foundry
# those aren't set directly; bind an AI Core service instance and this
# derives them from its VCAP_SERVICES entry, the same pattern fetch_data.py
# uses for the Destination service binding.
#
# Best-effort: the "aicore" VCAP label and the credentials/serviceurls shape
# below match SAP's documented AI Core service key format, but weren't
# fetched from live docs the way the rest of this file's APIs were --
# verify against your actual service key if AI Core calls fail with a clear
# auth/config error.
# --------------------------------------------------------------------------- #
def _configure_ai_core_env() -> None:
    if os.environ.get("AICORE_CLIENT_ID"):
        return  # explicit config (env vars or config.json) already present

    vcap_raw = os.environ.get("VCAP_SERVICES")
    if not vcap_raw:
        return

    try:
        vcap = json.loads(vcap_raw)
    except ValueError:
        return

    for entry in vcap.get("aicore", []):
        creds = entry.get("credentials", {})
        api_url = creds.get("serviceurls", {}).get("AI_API_URL")
        if not (creds.get("clientid") and creds.get("clientsecret") and creds.get("url") and api_url):
            continue
        os.environ["AICORE_CLIENT_ID"] = creds["clientid"]
        os.environ["AICORE_CLIENT_SECRET"] = creds["clientsecret"]
        os.environ["AICORE_AUTH_URL"] = creds["url"]
        os.environ["AICORE_BASE_URL"] = api_url.rstrip("/") + "/v2"
        os.environ.setdefault("AICORE_RESOURCE_GROUP", "default")
        return


_configure_ai_core_env()

# --------------------------------------------------------------------------- #
# data sources -- add more entries here as new paths become available
# --------------------------------------------------------------------------- #
DATA_SOURCES: dict[str, dict[str, Optional[str]]] = {
    "object_usage": {
        "description": (
            "Datasphere/BW object usage: which objects (DTPs, views, ...) were "
            "last used, when, and for which customer/tenant."
        ),
        "destination_name": os.environ.get("DESTINATION_NAME", "Datasphere_Joule"),
        "data_path": os.environ.get(
            "DATA_PATH",
            "api/v1/dwc/consumption/relational/ASSESSMENT/GV_CV_MASTER_OBJECT_USAGE/GV_CV_MASTER_OBJECT_USAGE",
        ),
        "data_query": os.environ.get("DATA_QUERY", "$top=10"),
    },
    # "another_path": {
    #     "description": "...",
    #     "destination_name": "Datasphere_Joule",
    #     "data_path": "api/v1/...",
    #     "data_query": "$top=50",
    # },
}


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@tool
def list_data_sources() -> str:
    """List the named Datasphere data sources available to fetch_dataset,
    with a description of what each one contains."""
    return "\n".join(f"- {name}: {cfg['description']}" for name, cfg in DATA_SOURCES.items())


@tool
def fetch_dataset(source: str) -> str:
    """Fetch the current rows for a named Datasphere data source.

    Call list_data_sources first if you don't already know the valid names.

    Args:
        source: the data source key, e.g. "object_usage".
    """
    cfg = DATA_SOURCES.get(source)
    if cfg is None:
        return f"Unknown source '{source}'. Valid sources: {', '.join(DATA_SOURCES)}"

    try:
        result = fetch_data.run_fetch(
            name=cfg["destination_name"],
            data_path=cfg["data_path"],
            data_query=cfg["data_query"],
        )
    except Exception as exc:  # noqa: BLE001 - surface as tool output, not a crash
        return f"Error fetching '{source}': {exc}"

    if not result["ok"]:
        body = result["data"] if result["data"] is not None else result["text"]
        return f"Fetch for '{source}' failed (HTTP {result['status_code']}): {body}"

    payload = result["data"] if result["data"] is not None else result["text"]
    rows = payload.get("value", payload) if isinstance(payload, dict) else payload
    # Cap so one fetch can't blow the model's context.
    return json.dumps(rows, indent=2)[:8000]


# --------------------------------------------------------------------------- #
# agent
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You answer questions about SAP Datasphere usage data. Use list_data_sources "
    "to see what's available, then fetch_dataset to pull the rows you need before "
    "answering. Base your answer only on the fetched data -- if it doesn't "
    "contain the answer, say so instead of guessing."
)

_agent = None


def get_agent():
    """Lazily build the agent (avoids the AI Core auth handshake at import
    time, e.g. before /health checks)."""
    global _agent
    if _agent is None:
        from gen_ai_hub.proxy import get_proxy_client
        from gen_ai_hub.proxy.langchain import init_llm

        # Verified locally against the installed SDK: init_llm always needs a
        # model_name (positional/main identification kwarg) *and* an
        # explicit proxy_client -- deployment_id alone isn't enough, and
        # proxy_client defaults to None (crashes with an unrelated
        # AttributeError if omitted). LLM_DEPLOYMENT_ID pins the exact
        # deployment when a resource group has more than one for that model.
        proxy_client = get_proxy_client("gen-ai-hub")
        model_name = os.environ.get("AI_CORE_MODEL_NAME", "gpt-4o")
        deployment_id = os.environ.get("LLM_DEPLOYMENT_ID")
        kwargs = {"proxy_client": proxy_client, "max_tokens": 4096}
        if deployment_id:
            kwargs["deployment_id"] = deployment_id
        model = init_llm(model_name, **kwargs)
        _agent = create_agent(
            model=model,
            tools=[list_data_sources, fetch_dataset],
            system_prompt=SYSTEM_PROMPT,
        )
    return _agent


def _final_text(result: dict) -> str:
    last = result["messages"][-1]
    return getattr(last, "content", str(last))


def ask(question: str) -> str:
    """Sync entry point (used by the plain REST /ask endpoint)."""
    result = get_agent().invoke({"messages": [{"role": "user", "content": question}]})
    return _final_text(result)


async def aask(question: str) -> str:
    """Async entry point (used by the A2A executor, to avoid blocking the loop)."""
    result = await get_agent().ainvoke({"messages": [{"role": "user", "content": question}]})
    return _final_text(result)
