from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from urllib import error, request

from fortify.cli.register.manifest import create_manifest
from fortify.cli.register.models import AgentManifest, AgentType

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_REGISTER_TIMEOUT = 5.0


def post_manifest(
    manifest: AgentManifest, *, timeout: float = DEFAULT_REGISTER_TIMEOUT
) -> dict:
    """POST a pre-built manifest to ``/v1/agents``. Returns the response dict.

    Split out from :func:`register_agent` so callers (e.g. ``fortify
    serve``'s auto-register flow) can build the manifest themselves —
    inspect it, log its name, then ship it.
    """
    api_key = os.environ.get("FORTIFY_KEY")
    if api_key is None:
        raise ValueError("FORTIFY_KEY must be set")
    api_url = os.environ.get("FORTIFY_API_URL", DEFAULT_API_URL)

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
