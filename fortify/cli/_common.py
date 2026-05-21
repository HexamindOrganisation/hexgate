"""Shared CLI building blocks used by both `fortify chat` and `fortify serve`."""

from __future__ import annotations

import argparse
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from fortify.agents.factory import AgentGraph, CallbackHandler
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
    approval_handler: Any = None,
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
    """Prompt the user in the terminal to approve one tool invocation."""
    arguments = decision.arguments or {}

    header = Text(f"Approval required for {decision.tool_name}", style="bold yellow")
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
    """Return a CLI approval handler — ``bool`` for auto modes, ``(Decision)
    -> bool`` for ``ask``."""
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
