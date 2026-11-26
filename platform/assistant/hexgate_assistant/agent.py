"""The assistant's one agent: its instructions, the MCP tools it may call, and
the limits every turn runs under."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import (
    AbstractToolset,
    FunctionToolset,
    ToolsetTool,
    WrapperToolset,
)
from pydantic_ai.usage import UsageLimits

from hexgate_assistant.changes import build_apply_tool
from hexgate_assistant.context import as_text, cap_tool_result
from hexgate_assistant.deps import TurnDeps

# The MCP tools the model is offered, all of them reads. The writes stay out:
# `policy_save` runs no checks, and `policy_apply` is called by our own code
# with the hashes read at turn start, never by the model.
ALLOWLIST = frozenset(
    {
        "policy_reference",
        "policy_list_files",
        "policy_read_file",
        "policy_resolve",
        "policy_list_roles",
        "policy_graph",
        "policy_check",
        "policy_dry_run",
        "policy_diff",
        "policy_risk_facts",
        "agents_list",
        "audit_decisions",
    }
)

# Per turn, passed with each run: a turn over any of these ends.
TURN_LIMITS = UsageLimits(
    request_limit=25,
    tool_calls_limit=40,
    input_tokens_limit=300_000,
)

# Per model request: 4 000 output tokens, and 120 s in all, every transport
# retry and the whole streamed answer included (`request_timeout`). The SDK's
# per-attempt `timeout` is kept at the same value, so it never ends first.
MODEL_SETTINGS = ModelSettings(max_tokens=4000, timeout=120, request_timeout=120)

# Refused calls in a row, per tool, before the turn ends.
TOOL_RETRIES = 3

# One MCP call. A call that runs over comes back to the model as the tool's
# result; a call the server refuses comes back as a retry (see `retries`).
MCP_CALL_SECONDS = 30

INSTRUCTIONS = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")


def build_agent(model: Model, mcp_url: str) -> Agent[TurnDeps, str]:
    """The agent, built once at start-up and shared by every turn."""
    return Agent(
        model,
        instructions=INSTRUCTIONS,
        deps_type=TurnDeps,
        tools=[build_apply_tool(mcp_url)],
        toolsets=[build_toolset(mcp_url)],
        model_settings=MODEL_SETTINGS,
        # A refused call (the server's refusal, an unknown tool, bad
        # arguments) uses a retry of that tool: the default of one would end
        # the turn on the second refusal in a row. Kept small, because a call
        # whose arguments the output-token cap cut off fails only once the
        # budget is spent: 4 attempts, then IncompleteToolCall.
        retries={"tools": TOOL_RETRIES},
    )


def function_tools(
    agent: Agent[TurnDeps, str],
) -> list[tuple[str, str | None, dict[str, Any]]]:
    """(name, description, input schema) of each tool registered on the
    agent itself (`apply_change`), offered beside the MCP server's."""
    return [
        (t.tool_def.name, t.tool_def.description, t.tool_def.parameters_json_schema)
        for toolset in agent.toolsets
        if isinstance(toolset, FunctionToolset)
        for t in toolset.tools.values()
    ]


def build_toolset(mcp_url: str) -> AbstractToolset[TurnDeps]:
    """The MCP server's tools, cut to ``ALLOWLIST``.

    The bearer token is a function of the run, so each turn opens its own
    MCP session with its own user's token; no session is shared between users.
    """
    # `read_timeout` bounds each MCP request; the agent's `tool_timeout`
    # would not, it only covers the agent's own function tools.
    mcp = MCPToolset(mcp_url, auth=_token, read_timeout=MCP_CALL_SECONDS)
    return CappedResults(mcp.filtered(_allowed))


@dataclass
class CappedResults(WrapperToolset[TurnDeps]):
    """Every tool result cut to what the model may receive (see
    `cap_tool_result`)."""

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[TurnDeps],
        tool: ToolsetTool[TurnDeps],
    ) -> Any:
        result = await super().call_tool(name, tool_args, ctx, tool)
        text = as_text(result)
        capped = cap_tool_result(name, text)
        return result if capped == text else capped


def _token(ctx: RunContext[TurnDeps]) -> str:
    return ctx.deps.token


def _allowed(_: RunContext[TurnDeps], tool: ToolDefinition) -> bool:
    return tool.name in ALLOWLIST
