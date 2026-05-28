"""Serve subcommand: bridge a local agent to the Fortify control plane.

Connects to `ws://{API_URL}/v1/projects/{project_id}/serve`, receives chat
messages sent by dashboard Playground tabs, runs the agent via the same
`stream_agent` engine the terminal chat uses, and ships every normalized
`StreamEvent` back over the socket.

Handles reconnection with exponential backoff so a backend bounce doesn't
permanently break the connection.

When a payload includes ``user_attenuation`` metadata (the Playground's
"Act as alice" affordance), the turn is wrapped in an ``async with User(...)``
scope. The runtime then lazily attenuates the agent's bound FortifyClient
token inside ``stream_agent`` — same code path a production dev's backend
uses when serving a real user.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from rich.console import Console
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from fortify.agents.factory import stream_agent
from fortify.bootstrap import bootstrap
from fortify.cli._common import (
    AgentRuntime,
    add_shared_agent_flags,
    build_approval_handler,
    build_runtime,
    load_agent_script,
)
from fortify.cli.state import ChatState
from fortify.cloud.client import FortifyConfig, resolve_agent_name
from fortify.runtime import User

logger = logging.getLogger(__name__)

RECONNECT_BASE = 1.0
RECONNECT_CAP = 15.0
PING_INTERVAL = 20.0


@dataclass
class ServeContext:
    """Runtime context required to service remote chat messages."""

    runtime: AgentRuntime
    state: ChatState


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
    """Receive loop for a single WebSocket session."""
    async with connect(url, ping_interval=PING_INTERVAL) as ws:
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

    Policy hot-reload is handled by the agent's attached :class:`~fortify.
    security.source.PolicySource`: ``stream_agent`` calls
    ``agent.refresh_policy()`` at the start of every turn, the source
    sends ``If-None-Match`` to the platform, and a ``304`` short-circuits
    to the cached bundle. No bespoke runtime rebuild needed here.
    """
    console = Console()
    config = FortifyConfig.from_env()
    base = config.base_url.rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        ws_base = "ws://" + base.removeprefix("http://")
    else:
        ws_base = f"ws://{base}"
    url = f"{ws_base}/v1/projects/{config.project_id}/serve"

    context = ServeContext(runtime=runtime, state=ChatState())
    backoff = RECONNECT_BASE

    console.print(
        f"[bold]fortify-serve[/] agent=[cyan]{runtime.agent_name}[/] project=[cyan]{config.project_id}[/]"
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
    """Register the `serve` subcommand on the top-level fortify CLI."""
    parser = subparsers.add_parser(
        "serve",
        help="Relay the local agent to the Fortify dashboard over WebSocket.",
        description=(
            "Serve a local agent to the Fortify dashboard Playground over "
            "WebSocket. Policy edits in the dashboard take effect at the next "
            "turn boundary."
        ),
    )
    add_shared_agent_flags(parser)
    parser.set_defaults(func=main)


def main(args: argparse.Namespace) -> int:
    """Entrypoint for the `fortify serve` subcommand."""
    console = Console()
    settings = bootstrap()
    base_dir = Path.cwd()

    if args.use:
        load_agent_script(args.use)

    # Serve mode routes through Fortify, not the local agent registry, so the
    # agent name resolves via FORTIFY_AGENT_NAME / "default" fallback when not
    # explicitly passed.
    agent_name = args.agent or resolve_agent_name()

    # `ask` doesn't make sense without a TTY for prompts. Coerce to
    # auto-approve unless the caller explicitly picked auto-deny.
    approval_mode = (
        args.approval_mode if args.approval_mode == "auto-deny" else "auto-approve"
    )
    approval_handler = build_approval_handler(console, approval_mode)

    runtime = build_runtime(
        settings,
        agent_name=agent_name,
        base_dir=base_dir,
        model=args.model,
        local_only=False,
        approval_handler=approval_handler,
    )

    asyncio.run(run_serve(runtime))
    return 0
