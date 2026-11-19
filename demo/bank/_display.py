"""Terminal output for the demo: show each tool call, its result, and the reply.

Kept out of bank_agent.py so the agent file stays short on camera.
"""

import json
import logging

from rich.console import Console

console = Console()

# Langfuse isn't configured for the demo; keep its startup warning off camera.
# Import it first: its logger module resets the level when it loads.
import langfuse.logger  # noqa: E402,F401

logging.getLogger("langfuse").setLevel(logging.CRITICAL)


def side_effect(line: str) -> None:
    """Print what a tool really did, in red, the moment it does it."""
    console.print(f"    [bold white on red] {line} [/]")


def show_run(result) -> None:
    """Print the run's tool calls and outputs in order, then the final reply."""
    console.print()
    for item in result.new_items:
        if item.type == "tool_call_item":
            raw = item.raw_item
            args = json.loads(getattr(raw, "arguments", "{}") or "{}")
            shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
            console.print(f"  [cyan]🔧 {raw.name}[/]([dim]{shown}[/])")
        elif item.type == "tool_call_output_item":
            out = str(item.output)
            denied = "denied" in out.lower() or "not allowed" in out.lower()
            style = "bold yellow" if denied else "green"
            mark = "⛔" if denied else "✓"
            console.print(f"     [{style}]{mark} {out}[/]")
    console.print()
    console.print(f"[bold]🤖 {result.final_output}[/]")
    console.print()
