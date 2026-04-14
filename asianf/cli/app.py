"""Inline terminal chat app for asianf."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from asianf.agent.factory import AgentGraph, CallbackHandler, create_agent, stream_agent
from asianf.cli.state import ChatState, LiveRunState, ToolActivity
from asianf.config.settings import Settings
from asianf.setup import bootstrap
from asianf.stream import ToolCallState
from asianf.tools.decorators import format_tool_call_label
from asianf.tools.fetch import fetch
from asianf.tools.websearch import web_search
from asianf.tracing.langfuse import maybe_get_trace_url


@dataclass
class AgentRuntime:
    """Bundle the runtime pieces needed by the terminal chat."""

    agent: AgentGraph
    handler: CallbackHandler
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


def _render_current_run(
    runtime: AgentRuntime,
    current_run: LiveRunState,
    trace_url: str | None = None,
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
    return Group(*_render_current_run(runtime, current_run, trace_url))


def _print_completed_turn(
    console: Console,
    runtime: AgentRuntime,
    current_run: LiveRunState,
    trace_url: str | None = None,
) -> None:
    """Print one completed assistant turn as stable terminal output."""
    console.print(_render_live_turn(runtime, current_run, trace_url))
    console.print()


def _build_runtime(settings: Settings) -> AgentRuntime:
    """Create the default runtime used by the terminal app."""
    tools = [web_search, fetch]
    agent, handler = create_agent(
        model=settings.model,
        tools=tools,
        session_id="asianf-cli",
        tags=["asianf", settings.search_engine, settings.model],
    )
    tools_by_name = {getattr(tool, "name", getattr(tool, "__name__", "tool")): tool for tool in tools}
    return AgentRuntime(agent=agent, handler=handler, tools_by_name=tools_by_name)


async def _chat_loop(console: Console, runtime: AgentRuntime) -> None:
    """Run the interactive terminal chat loop."""
    state = ChatState()

    console.print("[bold white]asianf[/] inline chat")
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
            console.print("[bold white]asianf[/] inline chat")
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
    settings = bootstrap()
    runtime = _build_runtime(settings)
    asyncio.run(_chat_loop(console, runtime))


if __name__ == "__main__":
    run()
