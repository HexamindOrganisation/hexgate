"""Load packaged and local agent definitions from disk."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml

from coolagents.agent.factory import AgentGraph, create_agent
from coolagents.agents.models import AgentSpec
from coolagents.security import AgentPolicy, load_policy
from coolagents.tools import fetch, web_search
from coolagents.tracing.langfuse import CallbackHandler

BUILTIN_TOOLS = {
    "fetch": fetch,
    "web_search": web_search,
}
AgentSource = Literal["builtin", "local"]


def builtin_agents_root() -> Path:
    """Return the filesystem path for packaged builtin agents."""
    return Path(str(files("coolagents.builtin_agents")))


def _load_agent_spec_from_dir(agent_dir: Path) -> AgentSpec:
    """Load an agent spec from a directory containing agent.yaml."""
    spec_path = agent_dir / "agent.yaml"
    payload = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    return AgentSpec.model_validate(payload)


def local_agents_root(base_dir: str | Path | None = None) -> Path:
    """Return the local project root used for agent discovery."""
    return Path(base_dir or Path.cwd())


def iter_local_agent_dirs(base_dir: str | Path | None = None) -> list[Path]:
    """Discover local agent directories in the project root and ./agents."""
    root = local_agents_root(base_dir)
    discovered: dict[Path, None] = {}

    for child in root.iterdir():
        if child.is_dir() and (child / "agent.yaml").exists():
            discovered[child] = None

    agents_dir = root / "agents"
    if agents_dir.exists():
        for child in agents_dir.iterdir():
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
) -> tuple[AgentGraph, CallbackHandler]:
    """Load and instantiate a packaged builtin agent."""
    spec = load_builtin_agent_spec(name)
    agent_dir = builtin_agents_root() / name
    system_prompt = (agent_dir / spec.system_prompt).read_text(encoding="utf-8")
    policy = load_policy(agent_dir / spec.policy)
    tools = resolve_builtin_tools(spec.tools, extra_tools=extra_tools)
    return create_agent(
        model=model or spec.model,
        tools=tools,
        system_prompt=system_prompt,
        policy=policy,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        name=spec.name,
    )


def load_local_agent(
    name: str,
    *,
    base_dir: str | Path | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Load and instantiate a local project agent."""
    spec = load_local_agent_spec(name, base_dir)
    agent_dir = find_local_agent_dir(name, base_dir)
    system_prompt = (agent_dir / spec.system_prompt).read_text(encoding="utf-8")
    policy = load_policy(agent_dir / spec.policy)
    tools = resolve_builtin_tools(spec.tools, extra_tools=extra_tools)
    return create_agent(
        model=model or spec.model,
        tools=tools,
        system_prompt=system_prompt,
        policy=policy,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        name=spec.name,
    )


def resolve_agent_source(name: str, base_dir: str | Path | None = None) -> AgentSource:
    """Return whether an agent id resolves from local or builtin definitions."""
    if name in list_local_agents(base_dir):
        return "local"
    if name in list_builtin_agents():
        return "builtin"
    raise KeyError(f'Unknown agent "{name}"')


def load_agent(
    name: str,
    *,
    base_dir: str | Path | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Load either a local or builtin agent by name."""
    if resolve_agent_source(name, base_dir) == "local":
        return load_local_agent(
            name,
            base_dir=base_dir,
            session_id=session_id,
            user_id=user_id,
            tags=tags,
            extra_tools=extra_tools,
            model=model,
        )
    return load_builtin_agent(
        name,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        extra_tools=extra_tools,
        model=model,
    )
