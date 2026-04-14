"""Inline terminal chat app for coolagents."""

from __future__ import annotations

import asyncio
import argparse
from dataclasses import dataclass
from pathlib import Path

from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from coolagents.agent.factory import AgentGraph, CallbackHandler, stream_agent
from coolagents.agents.loader import list_available_agents, load_agent, resolve_agent_source
from coolagents.cli.state import ChatState, LiveRunState, ToolActivity
from coolagents.config.settings import Settings
from coolagents.setup import bootstrap
from coolagents.stream import ToolCallState
from coolagents.tools import fetch, web_search
from coolagents.tools.decorators import format_tool_call_label
from coolagents.tracing.langfuse import maybe_get_trace_url

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
        return Spinner("dots", text="")
    if tool.status == ToolCallState.FAILED:
        return Text("✗", style="bold red")
    return Text("✓", style="bold green")


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

    for tool in current_run.tools:
        prefix = _tool_prefix(tool)
        if isinstance(prefix, Spinner):
            prefix.text = f" {_tool_summary(runtime, tool)}"
            renderables.append(prefix)
        else:
            renderables.append(
                Text.assemble(prefix, " ", (_tool_summary(runtime, tool), "white"))
            )

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


def _build_runtime(settings: Settings, *, agent_name: str, base_dir: Path, model: str | None) -> AgentRuntime:
    """Create the runtime used by the terminal app."""
    tools = [web_search, fetch]
    resolved_model = model or settings.model
    agent, handler = load_agent(
        agent_name,
        base_dir=base_dir,
        model=resolved_model,
        session_id="coolagents-cli",
        tags=["coolagents", settings.search_engine, resolved_model, agent_name],
        extra_tools={tool.name: tool for tool in tools},
    )
    tools_by_name = {getattr(tool, "name", getattr(tool, "__name__", "tool")): tool for tool in tools}
    return AgentRuntime(
        agent=agent,
        handler=handler,
        agent_name=agent_name,
        agent_source=resolve_agent_source(agent_name, base_dir),
        model=resolved_model,
        tools_by_name=tools_by_name,
    )


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the inline chat app."""
    parser = argparse.ArgumentParser(description="Run the coolagents inline chat CLI.")
    parser.add_argument("--agent", help="Agent id to load from local or builtin definitions.")
    parser.add_argument("--model", help="Optional model override for the selected agent.")
    parser.add_argument(
        "--list-agents",
        action="store_true",
        help="List available local and builtin agents, then exit.",
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


def _render_welcome(runtime: AgentRuntime) -> RenderableType:
    """Render a compact startup card for the terminal chat."""
    logo = Text(DOG_LOGO, style="bold bright_yellow", justify="center")
    info = Group(
        Text("coolagents", style="bold white"),
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

    if args.list_agents:
        for agent_name in list_available_agents(base_dir):
            source = resolve_agent_source(agent_name, base_dir)
            console.print(f"{agent_name} [{source}]")
        return

    agent_name = args.agent or _default_agent_name(base_dir)
    runtime = _build_runtime(settings, agent_name=agent_name, base_dir=base_dir, model=args.model)
    asyncio.run(_chat_loop(console, runtime))


if __name__ == "__main__":
    run()
