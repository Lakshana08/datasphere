"""
Wraps agent.aask() as an A2A (Agent2Agent) protocol server, so this can be
registered with an A2A-speaking caller -- e.g. a Joule pro-code agent -- as a
remote agent it delegates questions to.

Mounted onto the FastAPI app at /a2a (see app.py). Once mounted, other agents
discover it at:
  GET  <base>/a2a/.well-known/agent-card.json   -- the AgentCard
  POST <base>/a2a/ask                           -- JSON-RPC message/task endpoint

Built against the `a2a-sdk` package (from a2aproject/a2a-python), verified
against its current source at implementation time. That project is young and
its API has moved between versions -- if imports fail after
`pip install a2a-sdk`, run `python -c "import a2a, os; print(os.path.dirname(a2a.__file__))"`
and diff this file's imports against what's actually installed.
"""

from __future__ import annotations

import json
import os

from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, TaskState
from starlette.applications import Starlette

import agent as agent_module


class DatasphereAgentExecutor(AgentExecutor):
    """Wraps agent.aask() as an A2A AgentExecutor."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if not task:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Fetching Datasphere data and answering..."),
        )

        query = get_message_text(context.message)
        # Unhandled exceptions here are caught by the framework and turn the
        # task into TASK_STATE_ERROR automatically -- no manual try/except needed.
        answer = await agent_module.aask(query) if query else "No question text provided."

        await updater.add_artifact(parts=[new_text_part(text=answer, media_type="text/plain")])
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("Done."),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancel is not supported.")


def _default_base_url() -> str:
    """Best-effort public URL for this app: explicit PUBLIC_BASE_URL env var,
    else Cloud Foundry's VCAP_APPLICATION route, else localhost for local dev."""
    override = os.environ.get("PUBLIC_BASE_URL")
    if override:
        return override.rstrip("/")

    vcap_raw = os.environ.get("VCAP_APPLICATION")
    if vcap_raw:
        try:
            uris = json.loads(vcap_raw).get("application_uris") or []
            if uris:
                return f"https://{uris[0]}"
        except (ValueError, KeyError):
            pass

    return "http://localhost:8000"


PUBLIC_BASE_URL = _default_base_url()


def build_a2a_app(base_url: str) -> Starlette:
    skill = AgentSkill(
        id="query_datasphere_usage",
        name="Query Datasphere usage data",
        description=(
            "Answers natural-language questions about SAP Datasphere data "
            "(currently object usage; more sources will be added) by "
            "fetching live rows through the BTP Destination service and "
            "reasoning over them."
        ),
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["datasphere", "sap", "data"],
        examples=[
            "Which objects haven't been used in over a year?",
            "What's the most recently used object for CITGO BW 7.3?",
        ],
    )

    agent_card = AgentCard(
        name="Datasphere Fetch Agent",
        description="Fetches and answers questions about SAP Datasphere data.",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=f"{base_url}/a2a/ask",
                protocol_version="1.0",
            )
        ],
        skills=[skill],
    )

    request_handler = DefaultRequestHandler(
        agent_executor=DatasphereAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    routes = []
    routes.extend(create_agent_card_routes(agent_card))
    routes.extend(create_jsonrpc_routes(request_handler, "/ask"))
    return Starlette(routes=routes)
