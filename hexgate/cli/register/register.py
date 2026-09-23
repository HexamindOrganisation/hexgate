from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from urllib import error, request

from hexgate.cloud.client import HexgateError
from hexgate.config.env import resolve_api_key, resolve_api_url
from hexgate.manifest import create_manifest
from hexgate.manifest.models import AgentManifest, AgentType

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.tools import BaseTool

DEFAULT_REGISTER_TIMEOUT = 5.0
_log = logging.getLogger(__name__)


class AgentTreeCollision(HexgateError):
    """Two different agents in one tree share a name — registering both would
    conflate them under one ``(project, name)``. Raised by :func:`register_tree`
    unless ``force=True``."""


def post_manifest(
    manifest: AgentManifest, *, timeout: float = DEFAULT_REGISTER_TIMEOUT
) -> dict:
    """POST a pre-built manifest to ``/v1/agents``. Returns the response dict.

    Split out from :func:`register_agent` so callers (e.g. ``hexgate
    serve``'s auto-register flow) can build the manifest themselves —
    inspect it, log its name, then ship it.
    """
    api_key = resolve_api_key()
    if api_key is None:
        raise ValueError("HEXGATE_API_KEY must be set")
    api_url = resolve_api_url()

    payload = json.dumps({"manifest": manifest.model_dump()}).encode("utf-8")
    req = request.Request(
        f"{api_url}/v1/agents",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as e:
        raise ValueError(f"Failed to register agent: {e}") from e


def register_agent(
    agent: AgentType,
    *,
    description: str | None = None,
    tools: list[BaseTool] | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    timeout: float = DEFAULT_REGISTER_TIMEOUT,
) -> dict:
    """Create and register an agent manifest to platform /agents.

    `tools`, `model` and `system_prompt` are only consulted for LangChain graphs —
    every other framework reads them off the agent object directly.
    See `create_manifest` for the dispatch logic.
    """
    manifest = create_manifest(
        agent,
        description=description,
        tools=tools,
        model=model,
        system_prompt=system_prompt,
    )
    return post_manifest(manifest, timeout=timeout)


def register_tree(
    agent: AgentType,
    *,
    description: str | None = None,
    tools: list[BaseTool] | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    force: bool = False,
    timeout: float = DEFAULT_REGISTER_TIMEOUT,
    seen: dict[str, tuple[int, dict]] | None = None,
    on_register: Callable[[str, dict], None] | None = None,
    manifest: AgentManifest | None = None,
) -> dict:
    """Register ``agent`` and its whole sub-agent tree (post-order, memoized).

    Walks :func:`enumerate_subagents`; each sub-agent that exposes a live handle is
    registered *before* its parent, so a parent manifest only ever references already
    registered children. Idempotent (``post_manifest`` dedups by content_hash) and
    cycle-safe (each name is visited once). ``on_register(name, result)`` fires per
    posted node so a caller can log it.

    Fail-loud on a **collision** — the same name reached twice in one tree with a
    *different* manifest would conflate two agents under one ``(project, name)`` — by
    raising :class:`AgentTreeCollision`. ``force=True`` proceeds instead of failing:
    the first-seen manifest is kept and the colliding one is skipped (a cycle-safe
    walk memoizes each name, so it can't re-post one — force does not overwrite).

    ``description``/``tools``/``model``/``system_prompt`` apply to the *root* only;
    sub-agents are framework objects that introspect their own manifest. ``manifest``
    lets a caller pass the root's already-built manifest so it isn't rebuilt.

    Returns the root's ``post_manifest`` response.
    """
    from hexgate.security.naming import canonical_name

    seen = seen if seen is not None else {}
    if manifest is None:
        manifest = create_manifest(
            agent,
            description=description,
            tools=tools,
            model=model,
            system_prompt=system_prompt,
        )
    name = canonical_name(manifest.name)
    prior = seen.get(name)
    if prior is not None:
        prior_id, prior_dump = prior
        if prior_id == id(agent):
            # The SAME object revisited (a cycle, or a node shared with the root):
            # never a collision. Skip the manifest re-serialization the compare needs.
            return {}
        # A DIFFERENT object under the same name. Serialize now to compare.
        dump = manifest.model_dump(mode="json", exclude_none=True)
        if prior_dump == dump:
            return {}  # identical manifest → already registered under this name
        if not force:
            raise AgentTreeCollision(
                f"sub-agent tree has two different agents named {manifest.name!r}; "
                "registering both would conflate them under one (project, name). "
                "Give each a distinct name, or pass force=True to keep the "
                "first-seen one and skip this."
            )
        _log.warning(
            "register_tree: skipping agent %r — a different agent with that name was "
            "already registered in this tree (force=True keeps the first); its own "
            "sub-agents are still registered.",
            manifest.name,
        )
        # Skip POSTING the colliding node, but still walk its (distinct) sub-agents —
        # dropping that subtree would leave unrelated agents silently unregistered.
        _register_children(
            agent, force=force, timeout=timeout, seen=seen, on_register=on_register
        )
        return {}
    dump = manifest.model_dump(mode="json", exclude_none=True)
    seen[name] = (id(agent), dump)
    _register_children(
        agent, force=force, timeout=timeout, seen=seen, on_register=on_register
    )
    result = post_manifest(manifest, timeout=timeout)
    if on_register is not None:
        on_register(manifest.name, result)
    return result


def _register_children(
    agent: object,
    *,
    force: bool,
    timeout: float | None,
    seen: dict,
    on_register: "Callable[[str, dict], None] | None",
) -> None:
    """Walk each live-handle sub-agent (post-order). A name-only edge (bare Handoff /
    as_tool origin) has no handle to recurse into and is already on the parent manifest."""
    from hexgate.agents.enumeration import enumerate_subagents

    for link in enumerate_subagents(agent):
        if link.child is not None:
            register_tree(
                link.child,
                force=force,
                timeout=timeout,
                seen=seen,
                on_register=on_register,
            )
