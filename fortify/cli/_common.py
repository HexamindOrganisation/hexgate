"""Shared CLI building blocks used by both `fortify chat` and `fortify serve`."""

from __future__ import annotations

import argparse
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from fortify.agents.factory import AgentGraph, CallbackHandler
from fortify.agents.loader import load_agent, resolve_agent_source
from fortify.config.settings import Settings
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
) -> AgentRuntime:
    """Create the runtime used by the terminal app.

    ``local_only=True`` keeps the loader off the Fortify Cloud path even
    when ``FORTIFY_KEY`` is present in the environment.
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


def prompt_for_approval(
    console: Console,
    action: dict[str, object],
) -> bool:
    """Ask the user to approve one tool invocation in the terminal."""
    tool_name = str(action.get("tool_name", "tool"))
    arguments = action.get("arguments", {})

    console.print()
    console.print(
        Panel(
            Group(
                Text(f"Approval required for {tool_name}", style="bold yellow"),
                *(
                    Text(
                        f"{key}: {_truncate_approval_value(value)}",
                        style="white",
                    )
                    for key, value in (
                        arguments.items()
                        if isinstance(arguments, dict)
                        else [("arguments", arguments)]
                    )
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
    """Return the CLI approval handler for the selected mode."""
    if mode == "auto-approve":
        return True
    if mode == "auto-deny":
        return False

    def approval_handler(
        action: dict[str, object], _context: dict[str, object] | None
    ) -> bool:
        return prompt_for_approval(console, action)

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
