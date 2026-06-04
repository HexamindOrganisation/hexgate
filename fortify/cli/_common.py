"""Shared CLI building blocks used by both `fortify chat` and `fortify serve`."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from fortify.agents.factory import AgentGraph, ApprovalHandler, CallbackHandler
from fortify.agents.loader import load_agent, resolve_agent_source
from fortify.config.settings import Settings
from fortify.security.decision import Decision
from fortify.tools import fetch, web_search

ApprovalMode = Literal["ask", "auto-approve", "auto-deny"]


@dataclass
class AgentRuntime:
    """Bundle the runtime pieces needed by the terminal chat and serve loop."""

    agent: AgentGraph
    handler: CallbackHandler
    agent_name: str
    agent_source: str
    model: str
    tools_by_name: dict[str, object]


def build_runtime(
    settings: Settings,
    *,
    agent_name: str,
    base_dir: Path,
    model: str | None,
    local_only: bool = False,
    approval_handler: ApprovalHandler | None = None,
) -> AgentRuntime:
    """Create the runtime shared by ``fortify chat`` and ``fortify serve``.

    ``local_only=True`` keeps the loader off the Fortify Cloud path even
    when ``FORTIFY_KEY`` is present in the environment — what terminal
    chat uses, since it doesn't need cloud-fetched policy or a serve
    tunnel. ``fortify serve`` passes ``local_only=False`` so policy edits
    in the dashboard land at the next turn boundary. ``approval_handler``
    threads to :func:`load_agent` for inline ``NEEDS_APPROVAL`` resolution.
    """
    import os

    tools = [web_search, fetch]
    resolved_model = model or settings.model
    agent, handler = load_agent(
        agent_name,
        base_dir=base_dir,
        model=resolved_model,
        session_id="fortify-cli",
        tags=["fortify", settings.search_engine, resolved_model, agent_name],
        extra_tools={tool.name: tool for tool in tools},
        local_only=local_only,
        approval_handler=approval_handler,
    )
    runtime_tools = list(getattr(agent, "tools", [])) + list(tools)
    tools_by_name = {
        getattr(tool, "name", getattr(tool, "__name__", "tool")): tool
        for tool in runtime_tools
    }
    if not local_only and os.environ.get("FORTIFY_KEY"):
        agent_source = "fortify"
    else:
        agent_source = resolve_agent_source(agent_name, base_dir)
    return AgentRuntime(
        agent=agent,
        handler=handler,
        agent_name=agent_name,
        agent_source=agent_source,
        model=resolved_model,
        tools_by_name=tools_by_name,
    )


def build_runtime_from_local_agent(
    settings: Settings,
    *,
    agent_obj: Any,
    description: str | None,
    approval_handler: ApprovalHandler | None,
    auto_register: bool,
    console: Console,
) -> AgentRuntime:
    """Build an :class:`AgentRuntime` from a Python-loaded agent object.

    The uvicorn-style serve flow:
      1. ``create_manifest(agent_obj)`` — same dispatch ``fortify register``
         uses. FortifyAgent / OpenAI / Pydantic-AI agents introspect cleanly;
         raw LangGraph errors out with a clear message (the user should
         wrap with ``create_agent(...)`` or pass ``--tools`` to the legacy
         register flow).
      2. If ``auto_register`` and ``FORTIFY_KEY`` is set: POST the manifest
         to ``/v1/agents``. Idempotent — server short-circuits when the
         content_hash hasn't changed. Print "Registered" / "unchanged" so
         the operator sees what just happened.
      3. Fetch the cloud's policy YAML for this agent name via the bearer
         GET ``/v1/agents/{name}`` route. The operator may have edited it
         in the dashboard since last register; we pick up that edit.
      4. ``enforce_policy(agent_obj, policy, approval_handler=...)`` —
         wraps the LOCAL agent's tools with the cloud's policy. Local
         code stays authoritative for code (tools / model / prompt);
         platform is authoritative for policy.

    Returns an :class:`AgentRuntime` whose ``agent`` is the policy-wrapped
    FortifyAgent and ``agent_name`` is the manifest's name (matches what
    we'll announce to the relay's ``hello`` message).
    """
    import os

    import yaml

    from fortify.agents.factory import enforce_policy
    from fortify.cli.register.manifest import create_manifest
    from fortify.cli.register.register import post_manifest
    from fortify.cloud.client import FortifyClient, FortifyConfig
    from fortify.security.policy_set import load_policy_set_from_dict
    from fortify.tracing.langfuse import get_langfuse_handler

    manifest = create_manifest(agent_obj, description=description)
    agent_name = manifest.name

    if auto_register and os.environ.get("FORTIFY_KEY"):
        # Idempotent POST. ``created`` flag in the response distinguishes
        # "first registered" from "manifest unchanged" so we can give the
        # operator a meaningful console line.
        result = post_manifest(manifest)
        if result.get("created"):
            console.print(
                f"[dim]ℹ Registered agent[/] [cyan]{agent_name}[/] "
                f"[dim](v{result.get('version', '?')})[/]"
            )
        else:
            console.print(
                f"[dim]ℹ Agent[/] [cyan]{agent_name}[/] "
                f"[dim]already registered (manifest unchanged)[/]"
            )

    config = FortifyConfig.from_env()
    client = FortifyClient(config)
    payload, _etag = client.get_agent(agent_name)
    assert payload is not None, "first get_agent has no If-None-Match"

    policy_payload = yaml.safe_load(payload["policy_yaml"]) or {}
    policy = load_policy_set_from_dict(policy_payload)

    enforced = enforce_policy(
        agent_obj, policy, approval_handler=approval_handler
    )

    # Fresh handler for the streaming layer. The user's create_agent() call
    # built its own handler but discarded it; we make a new one bound to
    # this serve session's session_id so traces don't mix across runs.
    handler = get_langfuse_handler(
        session_id="fortify-serve",
        tags=["fortify", "fortify-serve", agent_name],
    )

    return AgentRuntime(
        agent=enforced,
        handler=handler,
        agent_name=agent_name,
        agent_source="fortify",
        model=settings.model,
        tools_by_name={
            getattr(t, "name", getattr(t, "__name__", "tool")): t
            for t in getattr(enforced, "tools", [])
        },
    )


def load_spec(spec: str) -> Any:
    """Resolve a ``module.path:attr`` spec to its target object.

    The shared loader for ``fortify register --agent <spec>`` and
    ``fortify serve <spec>`` — both subcommands take the same shape so
    devs only learn one form. ``file/path.py:attr`` works too via the
    leading ``sys.path.insert(0, '')`` (cwd) trick.

    Raises ``ValueError`` for malformed specs and ``AttributeError``
    for valid specs whose target object doesn't exist on the module.
    """
    module_path, sep, attr = spec.partition(":")
    if not sep or not module_path or not attr:
        raise ValueError(
            f"Invalid spec {spec!r}: expected 'module.path:attr' "
            f"(e.g. my_app.module:my_attr)"
        )

    # cwd on sys.path so a user can run from their project root and
    # spec their own module — same trick uvicorn / pytest pull.
    if "" not in sys.path:
        sys.path.insert(0, "")

    module = importlib.import_module(module_path)
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise AttributeError(
            f"Module {module_path!r} has no attribute {attr!r}"
        ) from exc


def load_agent_script(script_path: str | Path) -> Path:
    """Import a Python script that registers code-defined agents."""
    path = Path(script_path).expanduser().resolve()
    module_name = f"fortify_user_script_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path


def _truncate_approval_value(value: object, *, limit: int = 80) -> str:
    """Return a compact single-line representation for approval prompts."""
    text = str(value).replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def prompt_for_approval(console: Console, decision: Decision) -> bool:
    """Prompt the user in the terminal to approve one tool invocation.

    Reads everything from the :class:`Decision`: the proposed tool name,
    arguments, role, and agent_name. No external lookup needed.
    """
    arguments = decision.arguments or {}

    header = Text(
        f"Approval required for {decision.tool_name}", style="bold yellow"
    )
    role_line = (
        [Text(f"role: {decision.role}", style="dim")]
        if decision.role is not None
        else []
    )

    console.print()
    console.print(
        Panel(
            Group(
                header,
                *role_line,
                *(
                    Text(
                        f"{key}: {_truncate_approval_value(value)}",
                        style="white",
                    )
                    for key, value in arguments.items()
                ),
                Text("Type y to approve or n to deny, then press Enter.", style="dim"),
            ),
            border_style="yellow",
            title="[bold yellow]Approval[/]",
            padding=(0, 1),
        )
    )
    answer = console.input("[bold yellow]Approve? [y/N] [/]").strip().lower()
    console.print()
    return answer in {"y", "yes"}


def build_approval_handler(console: Console, mode: ApprovalMode):
    """Return a CLI approval handler — ``bool`` for auto modes, a
    ``(Decision) -> bool`` callable for ``ask``."""
    if mode == "auto-approve":
        return True
    if mode == "auto-deny":
        return False

    def approval_handler(decision: Decision) -> bool:
        return prompt_for_approval(console, decision)

    return approval_handler


def add_shared_agent_flags(parser: argparse.ArgumentParser) -> None:
    """Register flags shared between `fortify chat` and `fortify serve`."""
    parser.add_argument(
        "--agent", help="Agent id to load from local or builtin definitions."
    )
    parser.add_argument(
        "--model", help="Optional model override for the selected agent."
    )
    parser.add_argument(
        "--use",
        help="Python script that registers code-defined agents before loading --agent.",
    )
    parser.add_argument(
        "--approval-mode",
        choices=("ask", "auto-approve", "auto-deny"),
        default="ask",
        help="How the CLI should handle approval-required tools.",
    )
