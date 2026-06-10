"""Serve subcommand: bridge a local agent to the HexaGate control plane.

Connects to ``ws://{API_URL}/v1/serve`` and authenticates via the
``bearer.<envelope>`` WebSocket subprotocol — the server derives the
project from the bearer token (Phase 6, token-implicit project). The
``hexgate.v1`` marker subprotocol is offered alongside and must come
back echoed on the accepted handshake; a missing echo means the
platform is older than Phase 6 and we error out fast.

Receives chat messages sent by dashboard Playground tabs, runs the
agent via the same ``stream_agent`` engine the terminal chat uses,
and ships every normalized ``StreamEvent`` back over the socket.

Handles reconnection with exponential backoff so a backend bounce
doesn't permanently break the connection.

When a payload includes ``user_attenuation`` metadata (the Playground's
"Act as alice" affordance), the turn is wrapped in an ``async with
User(...)`` scope. The runtime then lazily attenuates the agent's
bound HexgateClient token inside ``stream_agent`` — same code path a
production dev's backend uses when serving a real user.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from pydantic import ValidationError
from rich.console import Console
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from hexgate.agents.factory import stream_agent
from hexgate.bootstrap import bootstrap
from hexgate.cli._common import (
    AgentRuntime,
    build_approval_handler,
    build_runtime_from_local_agent,
    load_spec,
)
from hexgate.cli.state import ChatState
from hexgate.cloud.client import HexgateConfig, HexgateError
from hexgate.runtime import User

logger = logging.getLogger(__name__)

RECONNECT_BASE = 1.0
RECONNECT_CAP = 15.0
PING_INTERVAL = 20.0
# Marker subprotocol the platform echoes back on a successful bearer
# handshake (matches ``_WS_PROTOCOL_MARKER`` in platform/api/main.py).
WS_PROTOCOL_MARKER = "hexgate.v1"


@dataclass
class ServeContext:
    """Runtime context required to service remote chat messages."""

    runtime: AgentRuntime
    state: ChatState
    # Bearer token used to build the WS subprotocol on each (re)connect.
    # Carried on the context so reconnect loops don't need to rebuild
    # the HexgateConfig on every retry.
    api_key: str


def _user_from_payload(attenuation: Any) -> User | None:
    """Build a :class:`User` from a chat payload's ``user_attenuation`` dict.

    Returns ``None`` (and logs a warning) when the payload is missing or
    malformed — the turn proceeds without an active User scope and the
    agent runs as if no attenuation was requested.
    """
    if not isinstance(attenuation, dict) or not attenuation.get("user"):
        return None
    try:
        return User(
            user_id=str(attenuation["user"]),
            role=attenuation.get("role"),
            session_id=attenuation.get("session_id"),
            ttl_seconds=attenuation.get("ttl_seconds"),
        )
    except (TypeError, ValueError, ValidationError) as exc:
        logger.warning("serve: invalid user_attenuation %r: %s", attenuation, exc)
        return None


@asynccontextmanager
async def _maybe_user_scope(user: User | None):
    """No-op async context manager when ``user`` is ``None``."""
    if user is None:
        yield
    else:
        async with user:
            yield


async def _handle_message(
    context: ServeContext,
    ws,
    payload: dict,
) -> None:
    """Dispatch a single inbound message from the chat peer."""
    kind = payload.get("type")

    if kind == "chat":
        text = str(payload.get("message", "")).strip()
        if not text:
            return
        # Policy refresh is handled inside stream_agent now (Phase 8a) —
        # the attached PolicySource sends If-None-Match and reuses the
        # cached bundle on 304. No need for serve to rebuild the runtime.
        context.state.start_turn(text)
        user = _user_from_payload(payload.get("user_attenuation"))
        async with _maybe_user_scope(user):
            async for event in stream_agent(
                context.runtime.agent,
                context.runtime.handler,
                context.state.build_input(),
            ):
                context.state.apply_event(event)
                await ws.send(event.model_dump_json())
        return

    if kind == "reset":
        context.state.clear()
        await ws.send(json.dumps({"type": "session_reset"}))
        return

    logger.warning("serve: ignoring unknown message type %r", kind)


async def _serve_loop(context: ServeContext, url: str, console: Console) -> None:
    """Receive loop for a single WebSocket session.

    Auth is via the ``bearer.<envelope>`` subprotocol — the server reads
    the token there, resolves the project, and rejects the handshake
    with close code 4401 on any failure. The ``hexgate.v1`` marker comes
    back echoed; an absent echo means we're talking to a pre-Phase-6
    platform and we bail out clean rather than running with no auth.

    The envelope is percent-encoded before being placed in the
    subprotocol value — the biscuit's base64 payload ends with ``=``
    padding, but WS subprotocols inherit the RFC 7230 token grammar
    which doesn't allow ``=``. The server unquotes it back on the
    other side; the grammar does allow ``%`` so percent-encoding
    survives the handshake intact.
    """
    bearer_value = quote(context.api_key, safe="")
    subprotocols = [f"bearer.{bearer_value}", WS_PROTOCOL_MARKER]
    async with connect(
        url, ping_interval=PING_INTERVAL, subprotocols=subprotocols
    ) as ws:
        if ws.subprotocol != WS_PROTOCOL_MARKER:
            raise HexgateError(
                f"platform did not negotiate the {WS_PROTOCOL_MARKER} "
                "subprotocol — deployment may be running an older API. "
                "Update the platform or pin to a matching hexgate CLI."
            )
        console.print(f"[green]connected[/] — relaying through {url}")
        await ws.send(
            json.dumps({"type": "hello", "agent": context.runtime.agent_name})
        )
        try:
            async for message in ws:
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("serve: ignoring non-JSON frame")
                    continue
                try:
                    await _handle_message(context, ws, payload)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("serve: error handling %r", payload.get("type"))
                    await ws.send(
                        json.dumps(
                            {
                                "event_type": "error",
                                "message": str(exc),
                                "run_id": "serve",
                                "root_run_id": "serve",
                                "sequence": 0,
                            }
                        )
                    )
        except ConnectionClosed:
            console.print("[yellow]disconnected[/]")


async def run_serve(runtime: AgentRuntime) -> None:
    """Top-level serve loop with reconnect + graceful shutdown.

    Policy hot-reload is handled by the agent's attached :class:`~hexgate.
    security.source.PolicySource`: ``stream_agent`` calls
    ``agent.refresh_policy()`` at the start of every turn, the source
    sends ``If-None-Match`` to the platform, and a ``304`` short-circuits
    to the cached bundle. No bespoke runtime rebuild needed here.
    """
    console = Console()
    config = HexgateConfig.from_env()
    base = config.base_url.rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        ws_base = "ws://" + base.removeprefix("http://")
    else:
        ws_base = f"ws://{base}"
    # No ``project_id`` in the URL — the bearer subprotocol carries it.
    url = f"{ws_base}/v1/serve"

    context = ServeContext(runtime=runtime, state=ChatState(), api_key=config.api_key)
    backoff = RECONNECT_BASE

    # ``project_id`` is best-effort display now (Phase 6); show a
    # placeholder when the envelope didn't carry it. The token itself
    # is the source of truth and the server logs the resolved project
    # on its side.
    project_display = config.project_id or "<from token>"
    console.print(
        f"[bold]hexgate-serve[/] agent=[cyan]{runtime.agent_name}[/] "
        f"project=[cyan]{project_display}[/]"
    )
    console.print("[dim]Ctrl+C to stop[/]")

    while True:
        try:
            await _serve_loop(context, url, console)
            backoff = RECONNECT_BASE
        except (ConnectionClosed, OSError) as exc:
            console.print(
                f"[yellow]connection lost[/] ({exc}); retrying in {backoff:.1f}s"
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_CAP)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]unexpected error:[/] {exc}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_CAP)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `serve` subcommand on the top-level hexgate CLI."""
    parser = subparsers.add_parser(
        "serve",
        help="Relay a local agent to the HexaGate dashboard over WebSocket.",
        description=(
            "Serve a local agent to the HexaGate dashboard Playground over "
            "WebSocket. Takes a module:attr spec — the same form as "
            "`hexgate register --agent ...` — and brings the agent up "
            "end-to-end: auto-registers the manifest (idempotent), fetches "
            "the cloud's policy, applies enforcement, then opens the relay. "
            "Policy edits in the dashboard take effect at the next turn."
        ),
    )
    parser.add_argument(
        "agent_spec",
        help=(
            "Agent to serve as module:attr — e.g. "
            "examples.customer_bot:agent. Same spec form as "
            "`hexgate register --agent ...`."
        ),
    )
    parser.add_argument(
        "--description",
        default=None,
        help="Optional description for the registered manifest.",
    )
    parser.add_argument(
        "--approval-mode",
        choices=("ask", "auto-approve", "auto-deny"),
        default="ask",
        help=(
            "How approval-required tool calls are handled. ``ask`` is the "
            "default and prompts in the terminal; serve coerces it to "
            "``auto-approve`` since the terminal isn't interactive during "
            "a WebSocket session."
        ),
    )
    parser.add_argument(
        "--no-auto-register",
        action="store_true",
        help=(
            "Skip the auto-register POST at startup. Errors if the agent "
            "isn't already on the platform. Useful for CI / deliberate "
            "deployments where registration is a separate step."
        ),
    )
    parser.set_defaults(func=main)


def main(args: argparse.Namespace) -> int:
    """Entrypoint for the `hexgate serve` subcommand.

    The uvicorn-style flow: load the agent object from a module:attr
    spec, derive a manifest from it (no flags needed — the object
    carries name, tools, model, and system_prompt), auto-register
    on the platform, fetch the operator's policy, and relay.
    """
    console = Console()
    settings = bootstrap()

    agent_obj = load_spec(args.agent_spec)

    # ``ask`` doesn't make sense in serve mode — no TTY prompt during
    # a relay session. Coerce to auto-approve unless the operator
    # explicitly picked auto-deny.
    approval_mode = (
        args.approval_mode if args.approval_mode == "auto-deny" else "auto-approve"
    )
    approval_handler = build_approval_handler(console, approval_mode)

    try:
        runtime = build_runtime_from_local_agent(
            settings,
            agent_obj=agent_obj,
            description=args.description,
            approval_handler=approval_handler,
            auto_register=not args.no_auto_register,
            console=console,
        )
    except HexgateError as exc:
        # Token + handshake + registration errors all bubble through
        # HexgateError; surface the message and exit cleanly.
        console.print(f"[red]✗[/] {exc}")
        return 1

    asyncio.run(run_serve(runtime))
    return 0
