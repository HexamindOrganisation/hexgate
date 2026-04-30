"""Inline terminal chat app for fortify."""

from __future__ import annotations

import asyncio
import argparse
from dataclasses import dataclass
import importlib.util
from pathlib import Path
from typing import Literal

from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from fortify import with_approval_handler
from fortify.agent.factory import AgentGraph, CallbackHandler, stream_agent
from fortify.agents.loader import list_available_agents, load_agent, resolve_agent_source
from fortify.cli.state import ChatState, LiveRunState, ToolActivity
from fortify.config.settings import Settings
from fortify.setup import bootstrap
from fortify.stream import ToolCallState
from fortify.tools import fetch, web_search
from fortify.tools.decorators import format_tool_call_label
from fortify.tracing.langfuse import maybe_get_trace_url

MAX_LIVE_RESPONSE_LINES = 12
MAX_LIVE_RESPONSE_CHARS = 2_400
DOG_LOGO = "\n".join(
    [
        " / \\__",
        "(    @\\___",
        " /         O",
        "/   (_____/",
        "/_____/   U",
    ]
)
ApprovalMode = Literal["ask", "auto-approve", "auto-deny"]


@dataclass
class AgentRuntime:
    """Bundle the runtime pieces needed by the terminal chat."""

    agent: AgentGraph
    handler: CallbackHandler
    agent_name: str
    agent_source: str
    model: str
    tools_by_name: dict[str, object]


def _tool_prefix(tool: ToolActivity) -> RenderableType:
    """Return a compact renderable prefix for a tool activity."""
    if tool.status == ToolCallState.STARTED:
        return Text("◉", style="bold cyan")
    if tool.status == ToolCallState.FAILED:
        return Text("◉", style="bold red")
    return Text("◉", style="bold green")


def _tool_summary(runtime: AgentRuntime, tool: ToolActivity) -> str:
    """Return a short one-line summary for a tool activity."""
    tool_object = runtime.tools_by_name.get(tool.tool_name)
    if tool_object is not None:
        return format_tool_call_label(tool_object, tool.arguments)
    return tool.tool_name


def _tail_text(
    text: str,
    *,
    max_lines: int = MAX_LIVE_RESPONSE_LINES,
    max_chars: int = MAX_LIVE_RESPONSE_CHARS,
) -> tuple[str, bool]:
    """Return the tail of a long text block for live terminal rendering."""
    truncated = False
    if len(text) > max_chars:
        text = text[-max_chars:]
        truncated = True

    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[-max_lines:])
        truncated = True

    return text.lstrip("\n"), truncated


def _render_current_run(
    runtime: AgentRuntime,
    current_run: LiveRunState,
    trace_url: str | None = None,
    *,
    live: bool = False,
) -> list[RenderableType]:
    """Render the active assistant turn inline in the transcript."""
    renderables: list[RenderableType] = []

    for index, tool in enumerate(current_run.tools):
        renderables.append(
            Text.assemble(_tool_prefix(tool), " ", (_tool_summary(runtime, tool), "white"))
        )
        if tool.summary and tool.status == ToolCallState.FAILED:
            renderables.append(Text(f"  {tool.summary}", style="dim red"))
        if index < len(current_run.tools) - 1:
            renderables.append(Text("│", style="dim white"))

    if current_run.reasoning_text.strip():
        renderables.append(Text(f"  {current_run.reasoning_text.rstrip()}", style="dim white"))

    if current_run.response_text.strip():
        if live:
            tailed_text, truncated = _tail_text(current_run.response_text.rstrip())
            if truncated:
                renderables.append(Text("...", style="dim white", justify="center"))
            renderables.append(Text(tailed_text, style="white"))
        else:
            renderables.append(Markdown(current_run.response_text.rstrip()))
    elif current_run.is_streaming:
        renderables.append(Spinner("dots", text=" thinking...", style="white"))

    if current_run.error:
        renderables.append(Text(f"  Error: {current_run.error}", style="bold red"))

    if trace_url and not current_run.is_streaming:
        renderables.append(Text(f"  Trace: {trace_url}", style="cyan"))

    return renderables


def _render_live_turn(
    runtime: AgentRuntime,
    current_run: LiveRunState,
    trace_url: str | None = None,
) -> RenderableType:
    """Render only the currently active assistant turn."""
    return Group(*_render_current_run(runtime, current_run, trace_url, live=True))


def _print_completed_turn(
    console: Console,
    runtime: AgentRuntime,
    current_run: LiveRunState,
    trace_url: str | None = None,
) -> None:
    """Print one completed assistant turn as stable terminal output."""
    console.print(Group(*_render_current_run(runtime, current_run, trace_url, live=False)))
    console.print()


def _build_runtime(
    settings: Settings,
    *,
    agent_name: str,
    base_dir: Path,
    model: str | None,
    local_only: bool = False,
) -> AgentRuntime:
    """Create the runtime used by the terminal app.

    ``local_only=True`` keeps the loader off the Fortify Cloud path even
    when ``FORTIFY_KEY`` is present in the environment — what terminal chat
    uses, since it doesn't need cloud-fetched policy or a serve tunnel.
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
        getattr(tool, "name", getattr(tool, "__name__", "tool")): tool for tool in runtime_tools
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


def _truncate_approval_value(value: object, *, limit: int = 80) -> str:
    """Return a compact single-line representation for approval prompts."""
    text = str(value).replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _prompt_for_approval(
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
                        arguments.items() if isinstance(arguments, dict) else [("arguments", arguments)]
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


def _build_approval_handler(
    console: Console,
    mode: ApprovalMode,
):
    """Return the CLI approval handler for the selected mode."""
    if mode == "auto-approve":
        return True
    if mode == "auto-deny":
        return False

    def approval_handler(action: dict[str, object], _context: dict[str, object] | None) -> bool:
        return _prompt_for_approval(console, action)

    return approval_handler


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the inline chat app."""
    parser = argparse.ArgumentParser(description="Run the fortify inline chat CLI.")
    parser.add_argument("--agent", help="Agent id to load from local or builtin definitions.")
    parser.add_argument("--model", help="Optional model override for the selected agent.")
    parser.add_argument(
        "--use",
        help="Python script that registers code-defined agents before loading --agent.",
    )
    parser.add_argument(
        "--list-agents",
        action="store_true",
        help="List available local and builtin agents, then exit.",
    )
    parser.add_argument(
        "--approval-mode",
        choices=("ask", "auto-approve", "auto-deny"),
        default="ask",
        help="How the CLI should handle approval-required tools.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help=(
            "Serve-mode: relay the agent to Fortify dashboard Playground tabs "
            "over WebSocket instead of running the interactive terminal chat."
        ),
    )
    return parser.parse_args()


def _default_agent_name(base_dir: Path) -> str:
    """Return the default agent id for the current project context."""
    available = list_available_agents(base_dir)
    if "example_agent" in available:
        return "example_agent"
    if "researcher" in available:
        return "researcher"
    if not available:
        raise RuntimeError("No agents found in the current project or builtin registry.")
    return available[0]


def _load_agent_script(script_path: str | Path) -> Path:
    """Import a Python script that registers code-defined agents."""
    path = Path(script_path).expanduser().resolve()
    module_name = f"fortify_user_script_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path


def _render_welcome(runtime: AgentRuntime) -> RenderableType:
    """Render a compact startup card for the terminal chat."""
    logo = Text(DOG_LOGO, style="bold bright_yellow", justify="center")
    info = Group(
        Text("fortify", style="bold white"),
        Text("a calm little terminal swarm", style="dim"),
        Text(""),
        Text(f"agent  {runtime.agent_name} ({runtime.agent_source})", style="white"),
        Text(f"model  {runtime.model}", style="white"),
        Text(""),
        Text("/clear  reset chat", style="dim"),
        Text("/exit   leave chat", style="dim"),
    )
    return Panel(
        Columns([logo, info], expand=True, equal=False),
        title="[bold bright_yellow]Welcome Back[/]",
        border_style="bright_yellow",
        padding=(1, 2),
    )


async def _chat_loop(console: Console, runtime: AgentRuntime) -> None:
    """Run the interactive terminal chat loop."""
    state = ChatState()

    console.print(_render_welcome(runtime))
    console.print("[dim]Ask a question. Use /clear to reset or /exit to quit.[/]")
    console.print()

    while True:
        user_text = console.input("[bold cyan]> [/]").strip()

        if not user_text:
            continue
        if user_text in {"/exit", "/quit"}:
            break
        if user_text == "/clear":
            state.clear()
            console.clear()
            console.print(_render_welcome(runtime))
            console.print("[dim]Ask a question. Use /clear to reset or /exit to quit.[/]")
            console.print()
            continue

        state.start_turn(user_text)
        current_run = state.current_run
        if current_run is None:
            continue

        with Live(
            _render_live_turn(runtime, current_run),
            console=console,
            refresh_per_second=20,
            transient=True,
        ) as live:
            async for event in stream_agent(runtime.agent, runtime.handler, state.build_input()):
                state.apply_event(event)
                if state.current_run is not None:
                    current_run = state.current_run
                    live.update(_render_live_turn(runtime, current_run))

            trace_url = maybe_get_trace_url(runtime.handler)
            current_run = state.current_run or current_run

        _print_completed_turn(console, runtime, current_run, trace_url)

        console.print()


def run() -> None:
    """Launch the inline terminal chat application."""
    console = Console()
    args = _parse_args()
    settings = bootstrap()
    base_dir = Path.cwd()

    if args.use:
        _load_agent_script(args.use)

    if args.list_agents:
        for agent_name in list_available_agents(base_dir):
            source = resolve_agent_source(agent_name, base_dir)
            console.print(f"{agent_name} [{source}]")
        return

    if args.serve:
        # Serve mode routes through Fortify, not the local agent registry,
        # so agent name resolves via FORTIFY_AGENT_NAME / "default" fallback.
        from fortify.cloud.client import resolve_agent_name

        agent_name = args.agent or resolve_agent_name()
    else:
        agent_name = args.agent or _default_agent_name(base_dir)

    # Terminal chat (no --serve) deliberately ignores FORTIFY_KEY: there's no
    # playground to feed and policy enforcement still works via the agent's own
    # YAML or registered factory. Serve mode keeps the cloud path.
    runtime = _build_runtime(
        settings,
        agent_name=agent_name,
        base_dir=base_dir,
        model=args.model,
        local_only=not args.serve,
    )

    if args.serve:
        # In serve mode, approval-ask doesn't make sense (no tty for prompts).
        # Coerce to auto-approve unless the caller explicitly picked auto-deny.
        approval_mode = args.approval_mode if args.approval_mode == "auto-deny" else "auto-approve"
        approval_handler = _build_approval_handler(console, approval_mode)

        def _wrap_for_serve(rt: AgentRuntime) -> AgentRuntime:
            rt.agent = with_approval_handler(
                rt.agent,
                approval_handler,
                context_provider=lambda: {"surface": "serve", "agent_name": rt.agent_name},
            )
            return rt

        runtime = _wrap_for_serve(runtime)

        def _rebuild() -> AgentRuntime:
            """Re-fetch YAMLs and rebuild the agent with the latest policy."""
            fresh = _build_runtime(
                settings,
                agent_name=agent_name,
                base_dir=base_dir,
                model=args.model,
                local_only=False,
            )
            return _wrap_for_serve(fresh)

        from fortify.cli.serve import run_serve

        asyncio.run(run_serve(runtime, rebuild=_rebuild))
        return

    runtime.agent = with_approval_handler(
        runtime.agent,
        _build_approval_handler(console, args.approval_mode),
        context_provider=lambda: {"surface": "cli", "agent_name": runtime.agent_name},
    )
    asyncio.run(_chat_loop(console, runtime))


if __name__ == "__main__":
    run()
