"""End-to-end verification that a real (wrapped) LangGraph agent's policy
comes from the live platform API and that its tool-call decision, its
LLM-usage event and the llm_message rows of its conversation all land in
ClickHouse.

Not a test of answer quality — the model is a scripted fake, never a real
LLM provider — this only proves Hexgate's own plumbing: policy fetch at
wrap time, tool-call auditing, and usage ingestion.

Requires the full OTLP ingest pipeline — Postgres, ClickHouse, Redpanda,
`make platform-api-pg`, `make collector-run` and `make enricher-run` — plus
`HEXGATE_API_KEY` set to a token minted against that Postgres-backed API.
`make platform-api` (SQLite) is NOT enough: the Collector authenticates
keys against the `devtoken` table in Postgres. See
.claude/skills/integration-tests for the exact sequence.

Opt in with: `pytest -m integration`.
"""

from __future__ import annotations

import json
import uuid

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from hexgate.adapters.langchain.wrapper import wrap_langchain_agent
from hexgate.cli.register.register import register_agent
from hexgate.runtime import HexgateContext
from tests.adapters.conftest import HexgatePlatformEnv
from tests.adapters.helpers import (
    AGENT_NAME_PREFIX,
    USER_ID_PREFIX,
    assert_policy_and_usage_events_landed,
    poll_until,
)

pytestmark = pytest.mark.integration

FAKE_MODEL_NAME = "hexgate-integration-fake-model"


class _ScriptedToolCallingModel(FakeMessagesListChatModel):
    """`FakeMessagesListChatModel` plus a no-op `bind_tools`.

    `create_agent` calls `model.bind_tools(tools)` once at graph-build
    time; the base `BaseChatModel.bind_tools` raises `NotImplementedError`
    and the fake never inspects tool schemas anyway (responses are
    pre-scripted), so identity is enough — no real provider, no network,
    no API key beyond `HEXGATE_API_KEY` for the platform.
    """

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # type: ignore[no-untyped-def]
        return self


def _make_get_weather_tool():
    """A `get_weather`-named tool: a "get_" prefix is a read-shape pattern
    (see `platform/api/hexgate_api/features/agents/compiler.py`'s
    `_READ_PATTERNS`), so a freshly-registered agent's starter policy puts
    it in the `read_only` mixin at `mode: allow` for every role — including
    the `default` role that an unrecognized `user_roles` entry falls back to.
    That makes the expected `policy_decision` outcome deterministic
    ('allow'), not something this test needs to special-case per role.

    Defined as an `async def` (not a sync `def`) so LangGraph's ToolNode
    calls the installed `coroutine` directly on the running event loop
    instead of via a thread-pool executor — the enforcer's `decide()`
    reads the active `HexgateContext` off a contextvar, which a plain
    `run_in_executor` thread would not see.
    """

    @tool
    async def get_weather(city: str) -> str:
        """Look up the weather for a city."""
        return f"{city}: sunny, 21C"

    return get_weather


def _scripted_responses() -> list[AIMessage]:
    """One tool-calling turn, then one final answer — deterministically
    triggers exactly one `get_weather` call and two `on_llm_end` events."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_weather",
                    "args": {"city": "Paris"},
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
            response_metadata={"model_name": FAKE_MODEL_NAME},
            usage_metadata={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
        ),
        AIMessage(
            content="It's sunny and 21C in Paris.",
            response_metadata={"model_name": FAKE_MODEL_NAME},
            usage_metadata={"input_tokens": 20, "output_tokens": 9, "total_tokens": 29},
        ),
    ]


@pytest.mark.asyncio
async def test_agent_run_lands_policy_decision_and_llm_usage_events(
    hexgate_platform_env: HexgatePlatformEnv,
) -> None:
    """Register + wrap a real minimal LangGraph agent, run it through a
    prompt that deterministically triggers one tool call, then poll
    ClickHouse for the resulting policy_decision and llm_invocation rows."""
    agent_name = f"{AGENT_NAME_PREFIX}langchain_{uuid.uuid4().hex[:8]}"
    session_id = f"s-{uuid.uuid4().hex[:8]}"

    tools = [_make_get_weather_tool()]
    model = _ScriptedToolCallingModel(responses=_scripted_responses())
    raw_agent = create_agent(model=model, tools=tools, name=agent_name)

    # `tools`/`model`/`system_prompt` are only consulted for LangChain
    # graphs — a compiled graph doesn't reliably expose them post-compile.
    register_agent(
        raw_agent,
        tools=tools,
        model=FAKE_MODEL_NAME,
        system_prompt="You are a test agent that exercises Hexgate's plumbing.",
    )
    # Policy is resolved from the platform right here, at wrap time
    # (fail-loud on a 404 if `register_agent` above didn't land first).
    wrapped = wrap_langchain_agent(
        agent=raw_agent, tools=tools, api_key=hexgate_platform_env.api_key
    )

    context = HexgateContext(
        user_id=f"{USER_ID_PREFIX}langchain",
        session_id=session_id,
        user_roles=["tester"],
    )
    result = await wrapped.ainvoke(
        {"messages": [{"role": "user", "content": "What's the weather in Paris?"}]},
        hexgate_context=context,
    )
    assert result["messages"][-1].content

    assert_policy_and_usage_events_landed(
        hexgate_platform_env, agent_name, session_id, "get_weather"
    )


def _transcript(
    env: HexgatePlatformEnv, agent_name: str, session_id: str
) -> list[dict]:
    """The run's llm_message rows, with the three JSON columns decoded. Polled,
    since the rows cross the Collector, Redpanda and the enricher first."""
    rows = poll_until(
        lambda: (
            r if len(r := env.llm_message_rows(agent_name, session_id)) >= 2 else None
        ),
        message="llm_message rows never landed in ClickHouse",
    )
    return [
        {
            **row,
            "input": json.loads(row["input_messages"]),
            "output": json.loads(row["output_messages"]),
            "system": json.loads(row["system_instructions"] or "null"),
        }
        for row in rows
    ]


@pytest.mark.asyncio
async def test_tool_calling_run_records_the_conversation_as_llm_messages(
    hexgate_platform_env: HexgatePlatformEnv,
) -> None:
    """The same two-turn run read back through `llm_message`, where the rows
    must concatenate into the conversation the agent actually saw.

    LangGraph hands the whole list to every call, so turn 2's input must be
    only what the graph appended rather than a second copy of the question,
    and the tool result in it is stored nowhere else.
    """
    agent_name = f"{AGENT_NAME_PREFIX}langchain_msg_{uuid.uuid4().hex[:8]}"
    session_id = f"s-{uuid.uuid4().hex[:8]}"

    tools = [_make_get_weather_tool()]
    model = _ScriptedToolCallingModel(responses=_scripted_responses())
    raw_agent = create_agent(
        model=model,
        tools=tools,
        name=agent_name,
        system_prompt="You are a weather assistant.",
    )
    register_agent(
        raw_agent,
        tools=tools,
        model=FAKE_MODEL_NAME,
        system_prompt="You are a weather assistant.",
    )
    wrapped = wrap_langchain_agent(
        agent=raw_agent, tools=tools, api_key=hexgate_platform_env.api_key
    )

    context = HexgateContext(
        user_id=f"{USER_ID_PREFIX}langchain",
        session_id=session_id,
        user_roles=["tester"],
    )
    result = await wrapped.ainvoke(
        {"messages": [{"role": "user", "content": "What's the weather in Paris?"}]},
        hexgate_context=context,
    )
    assert result["messages"][-1].content

    rows = _transcript(hexgate_platform_env, agent_name, session_id)

    assert len({row["turn_key"] for row in rows}) == 1, (
        "one graph run, one message list"
    )
    assert [row["message_seq"] for row in rows] == [0, 1], "no gap in the transcript"
    assert not any(row["resynced"] or row["truncated"] for row in rows)

    first, second = rows

    # Turn 1: question in, tool call out, with the system prompt lifted into
    # its own field on this row only.
    assert first["system"] == [
        {"type": "text", "content": "You are a weather assistant."}
    ]
    assert first["input"] == [
        {
            "role": "user",
            "parts": [{"type": "text", "content": "What's the weather in Paris?"}],
        }
    ]
    [tool_call] = first["output"][0]["parts"]
    assert tool_call["type"] == "tool_call"
    assert tool_call["name"] == "get_weather"
    assert tool_call["arguments"] == {"city": "Paris"}

    # Turn 2: only what the graph appended since, then the model's answer.
    assert second["system"] is None
    assert [m["role"] for m in second["input"]] == ["assistant", "tool"]
    assert second["input"][1]["parts"][0]["response"] == "Paris: sunny, 21C"
    assert second["output"] == [
        {
            "role": "assistant",
            "parts": [{"type": "text", "content": "It's sunny and 21C in Paris."}],
        }
    ]
