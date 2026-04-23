"""Load an agent from the Fortify control plane and apply policy enforcement."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml

from coolagents.agent.factory import AgentGraph, create_agent
from coolagents.agent.security import enforce_policy
from coolagents.agents.models import AgentSpec
from coolagents.fortify.client import FortifyClient, FortifyConfig, resolve_agent_name
from coolagents.security import AgentPolicy
from coolagents.tracing.langfuse import CallbackHandler


def load_fortify_agent(
    name: str | None = None,
    *,
    project_id: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Fetch an agent from Fortify and return it with policy enforcement applied.

    Agent name resolution: explicit arg → FORTIFY_AGENT_NAME env → "default".
    Every project is guaranteed to have a `default` agent, so zero-config use
    (set only FORTIFY_KEY) works.

    Mirrors `load_local_agent` but sources the three YAMLs (agent, policy, system)
    from the Fortify API instead of disk. Tool resolution and enforcement are
    identical — only the bytes' origin differs.
    """
    _ = user_id  # reserved for future user-scoped token attenuation

    resolved_name = resolve_agent_name(name)
    config = FortifyConfig.from_env(
        project_id=project_id, base_url=base_url, api_key=api_key
    )
    client = FortifyClient(config)
    payload = client.get_agent(resolved_name)

    spec = AgentSpec.model_validate(yaml.safe_load(payload["agent_yaml"]) or {})
    policy = AgentPolicy.model_validate(yaml.safe_load(payload["policy_yaml"]) or {})
    system_prompt = payload.get("system_md") or ""

    # Resolve tool references against the builtin registry (same path local uses).
    # Kept here rather than importing loader-private helpers to avoid a circular
    # import between coolagents.fortify and coolagents.agents.
    from coolagents.agents.loader import resolve_builtin_tools

    tools = resolve_builtin_tools(spec.tools, extra_tools=extra_tools)

    agent, handler = create_agent(
        model=model or spec.model,
        tools=tools,
        system_prompt=system_prompt,
        session_id=session_id,
        tags=tags or ["coolagents", "fortify", config.project_id],
        name=spec.name,
    )
    return enforce_policy(agent, policy), handler
