"""Serve-mode loop: bridge a local agent to the Fortify control plane.

Connects to `ws://{API_URL}/v1/projects/{project_id}/serve`, receives chat
messages sent by dashboard Playground tabs, runs the agent via the same
`stream_agent` engine the terminal chat uses, and ships every normalized
`StreamEvent` back over the socket.

Handles reconnection with exponential backoff so a backend bounce doesn't
permanently break the connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from fortify.agents.factory import stream_agent
from fortify.cli.state import ChatState
from fortify.cloud.attenuate import attenuate_for_user
from fortify.cloud.biscuit import (
    TokenError,
    TokenSignatureError,
    extract_facts,
    parse_envelope,
)
from fortify.cloud.client import FortifyClient, FortifyConfig
from fortify.runtime import ToolUseContext

logger = logging.getLogger(__name__)

RECONNECT_BASE = 1.0
RECONNECT_CAP = 15.0
PING_INTERVAL = 20.0


@dataclass
class ServeContext:
    """Runtime context required to service remote chat messages.

    ``client`` is a lazily-initialised :class:`FortifyClient` used to resolve
    the platform's public key once per process. Per-turn attenuation reuses
    that cached pubkey so each "act as <user>" message doesn't trigger a
    fresh JWKS fetch.
    """

    runtime: Any  # AgentRuntime from cli/app.py — avoid circular import
    state: ChatState
    rebuild: Callable[[], Any] | None = None  # returns a fresh AgentRuntime
    client: FortifyClient | None = None


def _build_attenuated_context(
    context: ServeContext, attenuation: dict[str, Any]
) -> ToolUseContext | None:
    """Attenuate the parent FORTIFY_KEY using ``attenuation`` metadata.

    Returns a :class:`ToolUseContext` whose ``biscuit_facts`` field carries
    the union of the parent's facts plus the per-user attribution the
    dashboard requested. Returns ``None`` and logs a warning when the dev's
    process isn't configured for cloud (no FORTIFY_KEY / no client) — the
    turn falls back to the runtime's default context.
    """
    if context.client is None:
        logger.warning(
            "serve: user_attenuation in payload but no FortifyClient — "
            "ignoring; agent will run with no per-user facts"
        )
        return None
    try:
        pub = context.client.public_key_bytes()
        child_envelope = attenuate_for_user(
            context.client.config.api_key,
            pub,
            user=str(attenuation["user"]),
            scope=list(attenuation.get("scope") or []) or None,
            limits=dict(attenuation.get("limits") or {}) or None,
            ttl_seconds=attenuation.get("ttl_seconds"),
        )
        _, _, biscuit_b64 = parse_envelope(child_envelope)
        facts = extract_facts(biscuit_b64, pub)
    except (TokenError, TokenSignatureError, KeyError, TypeError) as exc:
        logger.warning("serve: attenuation failed (%s); turn will run anyway", exc)
        return None
    return ToolUseContext(biscuit_facts=facts)


async def _refresh_runtime(context: ServeContext) -> None:
    """Rebuild the agent at turn start so policy edits land without a restart."""
    if context.rebuild is None:
        return
    try:
        context.runtime = await asyncio.to_thread(context.rebuild)
    except Exception as exc:  # noqa: BLE001
        logger.warning("serve: policy refresh failed, using stale runtime: %s", exc)


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
        await _refresh_runtime(context)
        context.state.start_turn(text)
        attenuation = payload.get("user_attenuation")
        tool_use_context: ToolUseContext | None = None
        if isinstance(attenuation, dict) and attenuation.get("user"):
            tool_use_context = _build_attenuated_context(context, attenuation)
        stream_kwargs = (
            {"tool_use_context": tool_use_context}
            if tool_use_context is not None
            else {}
        )
        async for event in stream_agent(
            context.runtime.agent,
            context.runtime.handler,
            context.state.build_input(),
            **stream_kwargs,
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


async def run_serve(runtime, *, rebuild: Callable[[], Any] | None = None) -> None:  # noqa: ANN001
    """Top-level serve loop with reconnect + graceful shutdown.

    If ``rebuild`` is provided, it will be invoked at the start of every chat
    turn to pick up policy edits made in the dashboard without a restart.
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

    # Build a FortifyClient up front so per-turn attenuation reuses one
    # JWKS fetch instead of paying it on every "act as <user>" turn. Local-
    # only flows (no FORTIFY_KEY) can't run user attenuation at all, so
    # leaving client=None is the right fallback there.
    try:
        client = FortifyClient(config)
    except Exception as exc:  # noqa: BLE001
        logger.info("serve: FortifyClient unavailable (%s); attenuation disabled", exc)
        client = None
    context = ServeContext(
        runtime=runtime, state=ChatState(), rebuild=rebuild, client=client
    )
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
