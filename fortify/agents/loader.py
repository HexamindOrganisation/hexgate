"""Load packaged and local agent definitions from disk."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, TypeAlias

import yaml

from fortify.agents.factory import AgentGraph, create_agent
from fortify.agents.factory import enforce_policy
from fortify.agents.models import AgentSpec
from fortify.cloud.client import FortifyClient, FortifyConfig, resolve_agent_name
from fortify.security import AgentPolicy, load_policy
from fortify.tools import (
    bash,
    edit_file,
    fetch,
    glob,
    grep,
    read_file,
    refund_order,
    web_search,
    write_file,
)
from fortify.tracing.langfuse import CallbackHandler

BUILTIN_TOOLS = {
    "bash": bash,
    "edit_file": edit_file,
    "fetch": fetch,
    "glob": glob,
    "grep": grep,
    "read_file": read_file,
    "refund_order": refund_order,
    "web_search": web_search,
    "write_file": write_file,
}
AgentSource = Literal["builtin", "local", "registered"]
AgentFactory: TypeAlias = Callable[..., tuple[AgentGraph, CallbackHandler]]
REGISTERED_AGENTS: dict[str, AgentFactory] = {}


def builtin_agents_root() -> Path:
    """Return the filesystem path for packaged builtin agents."""
    return Path(str(files("fortify.agents.builtin")))


def _load_agent_spec_from_dir(agent_dir: Path) -> AgentSpec:
    """Load an agent spec from a directory containing agent.yaml."""
    spec_path = agent_dir / "agent.yaml"
    payload = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    return AgentSpec.model_validate(payload)


def local_agents_root(base_dir: str | Path | None = None) -> Path:
    """Return the local project root used for agent discovery."""
    return Path(base_dir or Path.cwd())


def iter_local_agent_dirs(base_dir: str | Path | None = None) -> list[Path]:
    """Discover local agent directories in the project root, ./agents, and ./examples."""
    root = local_agents_root(base_dir)
    discovered: dict[Path, None] = {}

    for child in root.iterdir():
        if child.is_dir() and (child / "agent.yaml").exists():
            discovered[child] = None

    for sub in ("agents", "examples"):
        sub_dir = root / sub
        if sub_dir.exists():
            for child in sub_dir.iterdir():
                if child.is_dir() and (child / "agent.yaml").exists():
                    discovered[child] = None

    return sorted(discovered)


def list_local_agents(base_dir: str | Path | None = None) -> list[str]:
    """List locally discoverable project agents."""
    names: list[str] = []
    for agent_dir in iter_local_agent_dirs(base_dir):
        spec = _load_agent_spec_from_dir(agent_dir)
        names.append(spec.name)
    return sorted(names)


def register_agent(name: str, factory: AgentFactory) -> None:
    """Register a code-defined agent factory under a stable id."""
    REGISTERED_AGENTS[name] = factory


def unregister_agent(name: str) -> None:
    """Remove a previously registered code-defined agent."""
    REGISTERED_AGENTS.pop(name, None)


def clear_registered_agents() -> None:
    """Clear the in-memory code agent registry."""
    REGISTERED_AGENTS.clear()


def list_registered_agents() -> list[str]:
    """List currently registered code-defined agent ids."""
    return sorted(REGISTERED_AGENTS)


def list_builtin_agents() -> list[str]:
    """List available packaged builtin agent names."""
    root = builtin_agents_root()
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and (path / "agent.yaml").exists()
    )


def load_builtin_agent_spec(name: str) -> AgentSpec:
    """Load a builtin agent specification by name."""
    agent_dir = builtin_agents_root() / name
    return _load_agent_spec_from_dir(agent_dir)


def load_builtin_agent_policy(name: str) -> AgentPolicy:
    """Load the policy associated with a builtin agent."""
    spec = load_builtin_agent_spec(name)
    return load_policy((builtin_agents_root() / name / spec.policy))


def find_local_agent_dir(name: str, base_dir: str | Path | None = None) -> Path:
    """Resolve a local agent name to its directory."""
    for agent_dir in iter_local_agent_dirs(base_dir):
        spec = _load_agent_spec_from_dir(agent_dir)
        if spec.name == name:
            return agent_dir
    raise KeyError(f'Unknown local agent "{name}"')


def load_local_agent_spec(name: str, base_dir: str | Path | None = None) -> AgentSpec:
    """Load a local agent specification by name."""
    return _load_agent_spec_from_dir(find_local_agent_dir(name, base_dir))


def load_local_agent_policy(
    name: str,
    base_dir: str | Path | None = None,
) -> AgentPolicy:
    """Load the policy associated with a local agent."""
    spec = load_local_agent_spec(name, base_dir)
    return load_policy(find_local_agent_dir(name, base_dir) / spec.policy)


def list_available_agents(base_dir: str | Path | None = None) -> list[str]:
    """List merged local and builtin agent ids."""
    names = set(list_builtin_agents())
    names.update(list_local_agents(base_dir))
    names.update(list_registered_agents())
    return sorted(names)


def resolve_builtin_tools(
    tool_names: list[str],
    extra_tools: Mapping[str, Any] | None = None,
) -> list[Any]:
    """Resolve tool ids against builtin and user-provided tool registries."""
    registry = dict(BUILTIN_TOOLS)
    registry.update(extra_tools or {})

    resolved: list[Any] = []
    for tool_name in tool_names:
        try:
            resolved.append(registry[tool_name])
        except KeyError as exc:
            raise KeyError(f'Unknown tool "{tool_name}"') from exc
    return resolved


def load_builtin_agent(
    name: str,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
    approval_handler: Any = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Load and instantiate a packaged builtin agent."""
    spec = load_builtin_agent_spec(name)
    agent_dir = builtin_agents_root() / name
    system_prompt = (agent_dir / spec.system_prompt).read_text(encoding="utf-8")
    policy = load_policy(agent_dir / spec.policy)
    tools = resolve_builtin_tools(spec.tools, extra_tools=extra_tools)
    agent, handler = create_agent(
        model=model or spec.model,
        tools=tools,
        system_prompt=system_prompt,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        name=spec.name,
    )
    return enforce_policy(agent, policy, approval_handler=approval_handler), handler


def load_local_agent(
    name: str,
    *,
    base_dir: str | Path | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
    approval_handler: Any = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Load and instantiate a local project agent."""
    spec = load_local_agent_spec(name, base_dir)
    agent_dir = find_local_agent_dir(name, base_dir)
    system_prompt = (agent_dir / spec.system_prompt).read_text(encoding="utf-8")
    policy = load_policy(agent_dir / spec.policy)
    tools = resolve_builtin_tools(spec.tools, extra_tools=extra_tools)
    agent, handler = create_agent(
        model=model or spec.model,
        tools=tools,
        system_prompt=system_prompt,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        name=spec.name,
    )
    return enforce_policy(agent, policy, approval_handler=approval_handler), handler


def load_registered_agent(
    name: str,
    *,
    base_dir: str | Path | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
    approval_handler: Any = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Load a registered code-defined agent by id."""
    try:
        factory = REGISTERED_AGENTS[name]
    except KeyError as exc:
        raise KeyError(f'Unknown registered agent "{name}"') from exc
    agent, handler = factory(
        base_dir=base_dir,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        extra_tools=extra_tools,
        model=model,
    )
    # Registered factories don't know about the CLI's approval flow; layer it
    # on after the fact by re-stamping each policy-wrapped tool.
    if approval_handler is not None:
        agent = _apply_approval_handler(agent, approval_handler)
    return agent, handler


def _apply_approval_handler(agent: AgentGraph, approval_handler: Any) -> AgentGraph:
    """Re-stamp every PolicyEnforcer-backed GuardedTool with ``approval_handler``.

    Used for code-registered agents whose factories applied ``enforce_policy``
    internally without seeing the CLI's approval callback. Walks the agent's
    tools, finds new :class:`~fortify.adapters.langchain.tools.GuardedTool`
    instances, and re-wraps them via :meth:`GuardedTool.wrap` carrying the
    handler. Tools that aren't policy-enforced are left untouched.
    """
    from langchain_core.tools import BaseTool

    from fortify.adapters.langchain.tools import GuardedTool

    rewrapped: list[Any] = []
    touched = False
    for tool_spec in agent.tools:
        if isinstance(tool_spec, GuardedTool):
            rewrapped.append(
                GuardedTool.wrap(
                    tool_spec.wrapped_tool,
                    enforcer=tool_spec.enforcer,
                    approval_handler=approval_handler,
                )
            )
            touched = True
        elif isinstance(tool_spec, BaseTool):
            rewrapped.append(tool_spec)
        else:
            rewrapped.append(tool_spec)
    return agent.with_tools(rewrapped) if touched else agent


def resolve_agent_source(name: str, base_dir: str | Path | None = None) -> AgentSource:
    """Return whether an agent id resolves from local or builtin definitions."""
    if name in list_local_agents(base_dir):
        return "local"
    if name in list_registered_agents():
        return "registered"
    if name in list_builtin_agents():
        return "builtin"
    raise KeyError(f'Unknown agent "{name}"')


def load_fortify_agent(
    name: str | None = None,
    *,
    project_id: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
    approval_handler: Any = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Fetch an agent from Fortify and return it with policy enforcement applied.

    Agent name resolution: explicit arg → FORTIFY_AGENT_NAME env → "default".
    Every project is guaranteed to have a `default` agent, so zero-config use
    (set only FORTIFY_KEY) works.

    Mirrors `load_local_agent` but sources the three YAMLs (agent, policy, system)
    from the Fortify API instead of disk. Tool resolution and enforcement are
    identical — only the bytes' origin differs.

    The returned agent carries a ``fortify_client`` attribute referencing the
    :class:`~fortify.cloud.FortifyClient` used to fetch it; the runtime reads
    this attribute when an :class:`~fortify.runtime.User` scope is active to
    mint per-request attenuated tokens lazily.
    """
    from fortify.security.policy_set import load_policy_set_from_dict

    resolved_name = resolve_agent_name(name)
    config = FortifyConfig.from_env(
        project_id=project_id, base_url=base_url, api_key=api_key
    )
    client = FortifyClient(config)
    payload = client.get_agent(resolved_name)

    spec = AgentSpec.model_validate(yaml.safe_load(payload["agent_yaml"]) or {})
    system_prompt = payload.get("system_md") or ""

    # The platform returns one canonical ``policy.yaml`` text per agent. The
    # role bundle lives inline under a top-level ``roles:`` key when present;
    # otherwise the document is a flat single-policy doc. load_policy_set_from_dict
    # dispatches on shape — inheritance + mixin filtering applied either way.
    policy_payload = yaml.safe_load(payload["policy_yaml"]) or {}
    policy = load_policy_set_from_dict(policy_payload)

    tools = resolve_builtin_tools(spec.tools, extra_tools=extra_tools)

    agent, handler = create_agent(
        model=model or spec.model,
        tools=tools,
        system_prompt=system_prompt,
        session_id=session_id,
        tags=tags or ["fortify", "fortify-cloud", config.project_id],
        name=spec.name,
    )
    enforced = enforce_policy(agent, policy, approval_handler=approval_handler)
    # Attach the client so the runtime can do lazy attenuation inside an
    # active User scope without the caller having to thread it through.
    enforced.fortify_client = client
    return enforced, handler


def load_agent(
    name: str | None = None,
    *,
    base_dir: str | Path | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
    local_only: bool = False,
    approval_handler: Any = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Load an agent from Fortify (when FORTIFY_KEY is set), local, or builtin.

    When FORTIFY_KEY is set, `name` is optional: the SDK falls back to
    FORTIFY_AGENT_NAME and finally to `"default"`. For the local/builtin
    paths, `name` is required — we can't guess which local directory you
    meant.

    Pass ``local_only=True`` to force resolution from local / registered /
    builtin sources even when ``FORTIFY_KEY`` is set in the environment.
    Useful for terminal-chat workflows that don't need cloud-fetched policy.
    """
    if not local_only and os.environ.get("FORTIFY_KEY"):
        # load_fortify_agent dropped its reserved ``user_id`` placeholder
        # in phase 3.5 — per-request user identity comes from a User scope
        # at invocation time, not from the loader.
        return load_fortify_agent(
            name,
            session_id=session_id,
            tags=tags,
            extra_tools=extra_tools,
            model=model,
            approval_handler=approval_handler,
        )
    if name is None:
        raise ValueError("load_agent() requires a name when not using Fortify Cloud")
    source = resolve_agent_source(name, base_dir)
    if source == "local":
        return load_local_agent(
            name,
            base_dir=base_dir,
            session_id=session_id,
            user_id=user_id,
            tags=tags,
            extra_tools=extra_tools,
            model=model,
            approval_handler=approval_handler,
        )
    if source == "registered":
        return load_registered_agent(
            name,
            base_dir=base_dir,
            session_id=session_id,
            user_id=user_id,
            tags=tags,
            extra_tools=extra_tools,
            model=model,
            approval_handler=approval_handler,
        )
    return load_builtin_agent(
        name,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        extra_tools=extra_tools,
        model=model,
        approval_handler=approval_handler,
    )
