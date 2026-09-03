"""
Wraps agent.aask() as an A2A (Agent2Agent) protocol server, so this can be
registered with an A2A-speaking caller -- e.g. a Joule pro-code agent -- as a
remote agent it delegates questions to.

Mounted onto the FastAPI app at /a2a (see app.py). Once mounted, other agents
discover it at:
  GET  <base>/a2a/.well-known/agent-card.json   -- the AgentCard
  POST <base>/a2a                               -- JSON-RPC message/task endpoint

The BTP destination Joule resolves (system alias DATASPHERE_FETCH in
datasphere_fetch_capability/) must point at <base>/a2a -- the mount root,
NOT a deeper path -- so agent-card discovery and the JSON-RPC POST both
land here. See datasphere_fetch_capability/README.md.

Built against the `a2a-sdk` package (from a2aproject/a2a-python), verified
against its current source at implementation time. That project is young and
its API has moved between versions -- if imports fail after
`pip install a2a-sdk`, run `python -c "import a2a, os; print(os.path.dirname(a2a.__file__))"`
and diff this file's imports against what's actually installed.
"""

from __future__ import annotations

import json
import os

from a2a.helpers import get_message_text, new_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.utils.constants import DEFAULT_RPC_URL
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Task,
    TaskState,
    TaskStatus,
)
from starlette.applications import Starlette
from starlette.routing import Route

import agent as agent_module


class DatasphereAgentExecutor(AgentExecutor):
    """Wraps agent.aask() as an A2A AgentExecutor."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        query = get_message_text(context.message)
        # Unhandled exceptions here are caught by the framework and turn the
        # task into TASK_STATE_ERROR automatically -- no manual try/except needed.
        answer = await agent_module.aask(query) if query else "No question text provided."

        # Emit ONE already-completed Task carrying the answer in
        # status.message.parts[0].text. The Joule pro-code capability's
        # agent-request action reads exactly that path
        # (agentResult.body.status.message.parts[0].text). Enqueueing a
        # submitted/working Task first and completing it in a *separate*
        # status update lets Joule capture the pre-completion event (state
        # submitted/working, no message) as the result before the completed
        # update is applied. The full answer is already known by the time
        # this runs -- nothing streams -- so a single terminal Task is
        # correct and race-free. See smart_material_capability for the
        # debug trace that established this.
        agent_message = new_message(
            [new_text_part(answer)],
            context_id=context.context_id,
            task_id=context.task_id,
        )
        task = Task(
            id=context.task_id,
            context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED, message=agent_message),
        )
        await event_queue.enqueue_event(task)

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
                # The endpoint IS the mount root -- the same URL the BTP
                # destination points at -- so a caller that skips card
                # discovery and POSTs straight to the destination URL still
                # hits the JSON-RPC handler.
                url=f"{base_url}/a2a",
                protocol_version="1.0",
            )
        ],
        skills=[skill],
    )

    global _REQUEST_HANDLER
    _REQUEST_HANDLER = DefaultRequestHandler(
        agent_executor=DatasphereAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    routes = []
    routes.extend(create_agent_card_routes(agent_card))
    # rpc_url="/" -> served at the mount root (/a2a and /a2a/).
    # enable_v0_3_compat=True: Joule's agent-request calls the legacy
    # "message/send" JSON-RPC method with no A2A-Version header; without
    # this the server rejects it as an unsupported protocol version.
    routes.extend(
        create_jsonrpc_routes(_REQUEST_HANDLER, DEFAULT_RPC_URL, enable_v0_3_compat=True)
    )
    return Starlette(routes=routes)


_REQUEST_HANDLER: DefaultRequestHandler | None = None


def bare_rpc_route(mount_path: str = "/a2a") -> Route:
    """A POST route for the exact mount path (no trailing slash).

    app.mount("/a2a", ...) makes a bare POST to /a2a 307-redirect to /a2a/,
    and some A2A clients (Joule included) don't follow redirects on POST --
    the call just fails. Registering this directly on the parent app
    bypasses the Mount's redirect for that one case. Call build_a2a_app()
    first so the shared request handler exists.
    """
    if _REQUEST_HANDLER is None:
        raise RuntimeError("call build_a2a_app() before bare_rpc_route()")
    route = create_jsonrpc_routes(_REQUEST_HANDLER, mount_path, enable_v0_3_compat=True)[0]
    return Route(route.path, endpoint=route.endpoint, methods=["POST"])
