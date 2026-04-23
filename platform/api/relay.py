"""In-memory WebSocket relay for serve-mode agent sessions.

The backend bridges two peer roles per project:

- **serve** (the developer's `coolagents-chat --serve` process) — one per project.
  New connections replace any existing one; the old socket is closed.
- **chat** (dashboard Playground tabs) — many per project. All chat sockets
  receive the same events from the serve peer.

Messages are opaque JSON blobs relayed unchanged. Synthetic events are
injected by the relay itself: `{"type": "agent_online", "online": bool}`
so the dashboard can render a live/offline indicator without polling.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket


@dataclass
class ProjectRelay:
    serve: WebSocket | None = None
    chats: set[WebSocket] = field(default_factory=set)


class ConnectionRegistry:
    """Track serve/chat WebSockets per project and broker messages."""

    def __init__(self) -> None:
        self._projects: dict[str, ProjectRelay] = defaultdict(ProjectRelay)
        self._lock = asyncio.Lock()

    async def attach_serve(self, project_id: str, ws: WebSocket) -> WebSocket | None:
        """Register a serve socket, evicting any previous one."""
        async with self._lock:
            entry = self._projects[project_id]
            previous = entry.serve
            entry.serve = ws
        if previous is not None and previous is not ws:
            try:
                await previous.close(code=1000, reason="replaced by new serve connection")
            except Exception:
                pass
        await self._broadcast_chat(project_id, {"type": "agent_online", "online": True})
        return previous

    async def detach_serve(self, project_id: str, ws: WebSocket) -> None:
        async with self._lock:
            entry = self._projects.get(project_id)
            if entry is None or entry.serve is not ws:
                return
            entry.serve = None
        await self._broadcast_chat(project_id, {"type": "agent_online", "online": False})

    async def attach_chat(self, project_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._projects[project_id].chats.add(ws)
            online = self._projects[project_id].serve is not None
        await _safe_send(ws, {"type": "agent_online", "online": online})

    async def detach_chat(self, project_id: str, ws: WebSocket) -> None:
        async with self._lock:
            entry = self._projects.get(project_id)
            if entry is not None:
                entry.chats.discard(ws)

    async def relay_to_serve(self, project_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            serve = self._projects.get(project_id, ProjectRelay()).serve
        if serve is None:
            return
        await _safe_send(serve, payload)

    async def relay_to_chat(self, project_id: str, payload: dict[str, Any]) -> None:
        await self._broadcast_chat(project_id, payload)

    async def _broadcast_chat(self, project_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            chats = list(self._projects.get(project_id, ProjectRelay()).chats)
        for ws in chats:
            await _safe_send(ws, payload)


async def _safe_send(ws: WebSocket, payload: dict[str, Any]) -> bool:
    """Send JSON, swallow socket errors, return success."""
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        return False


registry = ConnectionRegistry()
