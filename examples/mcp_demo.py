"""Self-contained MCP-proxy demo — one file, no external services.

This script wears two hats. Run with ``--server`` to act as a tiny MCP
server (FastMCP, stdio transport) exposing three demo tools; run
without arguments to act as the *client* — it spawns itself as a
subprocess to play the server role, connects via
:class:`hexgate.mcp.MCPToolset`, wraps the tools with the policy
defined inline below, and walks through three calls to demonstrate
the allow / deny / approval-required outcomes.

No LLM required. The example exercises the policy seam directly so it
runs in CI and works without an OpenAI key. The final block (commented
out) shows how to upgrade to a real agent loop with ``create_agent``
+ ``enforce_policy``.

Usage::

    uv run python examples/mcp_demo.py
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Server side — runs when this file is invoked with ``--server``.
# ---------------------------------------------------------------------------


def run_server() -> None:
    """Tiny FastMCP server with three demo tools.

    Three modes so the demo policy below can map one tool per outcome:
      * ``compute_tip`` — math, always safe → allow
      * ``read_secret`` — fakes a secret read → deny
      * ``send_invoice`` — fakes a side-effect → approval_required
    """
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("demo-server")

    @server.tool(description="Compute the tip on a bill amount (USD).")
    def compute_tip(amount: float, percent: float = 18.0) -> str:
        tip = round(amount * percent / 100, 2)
        return f"tip on ${amount:.2f} at {percent}% = ${tip:.2f}"

    @server.tool(
        description="Read a stored secret by key (DEMO — never call in real life)."
    )
    def read_secret(key: str) -> str:
        return f"secret['{key}'] = hunter2"  # fake; demo only

    @server.tool(
        description="Send an invoice for an order. Returns the queued invoice id."
    )
    def send_invoice(order_id: str, amount: float) -> str:
        return f"queued invoice for {order_id} (${amount:.2f}) — id=INV-12345"

    server.run("stdio")


# ---------------------------------------------------------------------------
# Client side — runs when this file is invoked without arguments.
# ---------------------------------------------------------------------------


# Demo policy, written inline so the example is one-file. In a real
# project this would be a separate ``policy.yaml``.
POLICY_YAML = textwrap.dedent(
    """
    version: 1
    roles:
      default:
        default_policy:
          mode: deny
        tools:
          "mcp-demo-compute_tip":
            mode: allow
          "mcp-demo-read_secret":
            mode: deny
          "mcp-demo-send_invoice":
            mode: approval_required
    """
).strip()


def _heading(title: str) -> None:
    bar = "─" * (len(title) + 2)
    print(f"\n┌{bar}┐\n│ {title} │\n└{bar}┘")


async def _call_with_label(tool: Any, label: str, args: dict[str, Any]) -> None:
    """Invoke a wrapped tool + render the outcome (success / deny / approval).

    GuardedTool always RETURNS — never raises — so the agent's loop can
    feed the structured ``{"ok": False, "error": {...}}`` back to the
    LLM as tool output, letting the model adapt rather than crashing
    mid-run. We discriminate here by inspecting that envelope.
    """
    print(f"\n→ {label}: {tool.name}({args})")
    result = await tool.ainvoke(args)
    if isinstance(result, dict) and not result.get("ok", True):
        err = result.get("error", {})
        kind = err.get("type", "unknown")
        msg = err.get("message", "")
        print(f"  ✗ {kind} → {msg}")
    else:
        print(f"  ✓ allowed → {result!r}")


async def main() -> None:
    import yaml

    from hexgate.adapters.langchain.tools import GuardedTool
    from hexgate.mcp import MCPServerConfig, MCPToolset
    from hexgate.security.enforcer import build_enforcer
    from hexgate.security.policy_set import load_policy_set_from_dict

    _heading("1. Attach the MCP server")
    server_config = MCPServerConfig(
        name="demo",
        transport="stdio",
        # Re-exec this file with ``--server`` as the MCP server subprocess.
        # In a real project the command would be the third-party server,
        # e.g. command="slack-mcp-server" or
        # command="npx", args=("@modelcontextprotocol/server-filesystem", "/tmp").
        command=sys.executable,
        args=(str(Path(__file__).resolve()), "--server"),
    )
    print(f"  config: {server_config}")

    async with MCPToolset(server_config) as mcp:
        _heading("2. Tools auto-registered under the mcp-<server>-<tool> namespace")
        for t in mcp.tools:
            print(f"  • {t.name:<30} — {(t.description or '').splitlines()[0][:60]}")

        _heading("3. Wrap them with a policy")
        # In a real script you'd call `enforce_policy(agent, policy_path)` —
        # which builds the enforcer and wraps every tool on the agent in
        # GuardedTool. Here we skip the agent entirely (no LLM key needed
        # for this demo) and wrap each MCP tool by hand using the same
        # GuardedTool the framework adapters use.
        engine = load_policy_set_from_dict(yaml.safe_load(POLICY_YAML))
        enforcer = build_enforcer(engine, agent_name="demo")
        wrapped = {
            t.name: GuardedTool.wrap(t, enforcer=enforcer, approval_handler=None)
            for t in mcp.tools
        }

        _heading("4. Walk through one call per policy outcome")
        await _call_with_label(
            wrapped["mcp-demo-compute_tip"],
            "allow",
            {"amount": 42.50, "percent": 20},
        )
        await _call_with_label(
            wrapped["mcp-demo-read_secret"],
            "deny",
            {"key": "production_db_password"},
        )
        await _call_with_label(
            wrapped["mcp-demo-send_invoice"],
            "approval_required",
            {"order_id": "ORD-7", "amount": 199.0},
        )

    _heading("Done")
    print(
        "  In a real agent: drop `wrapped` into create_agent(tools=...) and\n"
        "  enforce_policy() will gate the LLM's tool calls the same way."
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--server":
        run_server()
    else:
        asyncio.run(main())
