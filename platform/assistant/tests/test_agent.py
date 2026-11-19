import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from hexgate_assistant import agent as agent_module
from hexgate_assistant.agent import (
    ALLOWLIST,
    INSTRUCTIONS,
    TURN_LIMITS,
    TurnDeps,
    build_agent,
)
from tests.fake_mcp import FakeMCP, serve, snapshot_tools

pytestmark = pytest.mark.anyio

LIST_FILES = ToolCallPart("policy_list_files", {"project_id": "p1"})


@pytest.fixture(scope="module")
async def server():
    async with serve() as state:
        yield state


@pytest.fixture
def mcp(server: FakeMCP) -> FakeMCP:
    server.calls.clear()
    server.delay = 0.0
    return server


def call_once(call: ToolCallPart, seen: list[AgentInfo] | None = None) -> FunctionModel:
    """A model that makes ``call``, then answers with what came back."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append(info)
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        for part in last.parts:
            if isinstance(part, ToolReturnPart):
                return ModelResponse(parts=[TextPart(part.model_response_str())])
            if isinstance(part, RetryPromptPart):
                return ModelResponse(parts=[TextPart(part.model_response())])
        return ModelResponse(parts=[call])

    return FunctionModel(respond)


async def run(model: FunctionModel, mcp: FakeMCP, token: str = "tok-a") -> str:
    agent = build_agent(model, mcp.url)
    result = await agent.run("hi", deps=TurnDeps(token), usage_limits=TURN_LIMITS)
    return result.output


def test_turn_limits_happy_path():
    assert TURN_LIMITS.request_limit == 25
    assert TURN_LIMITS.tool_calls_limit == 40
    assert TURN_LIMITS.input_tokens_limit == 300_000


def test_allowlist_happy_path():
    assert ALLOWLIST == {t["name"] for t in snapshot_tools()}
    assert "policy_apply" not in ALLOWLIST
    assert "policy_save" not in ALLOWLIST


async def test_when_the_model_calls_a_tool_then_mcp_runs_it_with_the_turn_token(
    mcp: FakeMCP,
):
    seen: list[AgentInfo] = []

    output = await run(call_once(LIST_FILES, seen), mcp)

    offered = {t.name: t.parameters_json_schema for t in seen[0].function_tools}
    assert offered == {t["name"]: t["inputSchema"] for t in snapshot_tools()}
    assert [(c.tool, c.arguments, c.authorization) for c in mcp.calls] == [
        ("policy_list_files", {"project_id": "p1"}, "Bearer tok-a")
    ]
    assert output == "policy_list_files result"


async def test_when_two_turns_run_then_each_calls_mcp_with_its_own_token(
    mcp: FakeMCP,
):
    model = call_once(LIST_FILES)

    await run(model, mcp, token="tok-a")
    await run(model, mcp, token="tok-b")

    assert [c.authorization for c in mcp.calls] == ["Bearer tok-a", "Bearer tok-b"]


@pytest.mark.parametrize("tool", ["policy_apply", "policy_save"])
async def test_when_the_model_calls_a_write_tool_then_mcp_never_receives_it(
    mcp: FakeMCP, tool: str
):
    output = await run(call_once(ToolCallPart(tool, {"project_id": "p1"})), mcp)

    assert mcp.calls == []
    assert f"Unknown tool name: '{tool}'" in output


async def test_when_a_turn_runs_then_the_model_gets_the_prompt_and_the_model_settings(
    mcp: FakeMCP,
):
    seen: list[ModelRequest] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert isinstance(messages[-1], ModelRequest)
        seen.append(messages[-1])
        assert info.model_settings is not None
        assert info.model_settings.get("max_tokens") == 4000
        assert info.model_settings.get("timeout") == 120
        return ModelResponse(parts=[TextPart("ok")])

    await run(FunctionModel(respond), mcp)

    assert seen[0].instructions == INSTRUCTIONS.strip()


async def test_when_the_model_never_stops_calling_tools_then_the_turn_ends_at_25_requests(
    mcp: FakeMCP,
):
    model = FunctionModel(lambda _messages, _info: ModelResponse(parts=[LIST_FILES]))

    with pytest.raises(UsageLimitExceeded, match="request_limit of 25"):
        await run(model, mcp)

    assert len(mcp.calls) == 25


async def test_when_an_mcp_call_runs_over_its_time_then_the_model_is_told_and_the_turn_goes_on(
    mcp: FakeMCP, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(agent_module, "MCP_CALL_SECONDS", 0.2)
    mcp.delay = 2

    output = await run(call_once(LIST_FILES), mcp)

    assert "timed out" in output


async def test_when_the_model_calls_an_unknown_tool_again_and_again_then_the_turn_goes_on(
    mcp: FakeMCP,
):
    refusals: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        refusals.extend(
            p.model_response_str() for p in last.parts if isinstance(p, ToolReturnPart)
        )
        if len(refusals) < 3:
            return ModelResponse(parts=[ToolCallPart("policy_apply", {})])
        return ModelResponse(parts=[TextPart("done")])

    output = await run(FunctionModel(respond), mcp)

    assert output == "done"
    assert len(refusals) == 3
    assert all("Unknown tool name: 'policy_apply'" in r for r in refusals)
    assert mcp.calls == []


def test_when_turn_deps_are_printed_then_the_token_is_not():
    assert "tok-secret" not in repr(TurnDeps("tok-secret"))
