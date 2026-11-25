"""The turn, end to end: the route, the runner and its stream, over the
framework's function model and a fake MCP server."""

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from pydantic_ai.exceptions import ModelContextWindowExceededError, ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from sqlalchemy import select

from hexgate_assistant import agent as agent_module
from hexgate_assistant import runner
from hexgate_assistant.auth import AssistantToken, JwksTokenVerifier
from hexgate_assistant.context import Editor, estimate_tokens
from hexgate_assistant.jwks import JwksCache
from hexgate_assistant.models import (
    AssistantMessage,
    AssistantSession,
    AssistantTurnLock,
    AssistantUsage,
)
from hexgate_assistant.server import GRACEFUL_SECONDS, SHUTDOWN_SECONDS, create_app
from hexgate_assistant.sessions import SessionStore
from tests.conftest import JWKS_URL, ORG, OTHER_PROJECT, PROJECT, SUB, jwks
from tests.fake_mcp import FakeMCP, serve

TURNS = f"/v1/assistant/projects/{PROJECT}/turns"
FILES = json.dumps(
    {
        "files": [
            {"name": "policy.yaml", "text_hash": "h-policy"},
            {"name": "caps/refund.yaml", "text_hash": "h-refund"},
        ],
        "modules": [],
    }
)
GRAPH = json.dumps({"roles": ["support"], "declared_agents": ["billing"]})
READ_FILE = ToolCallPart("policy_read_file", {"project_id": PROJECT, "name": "x"}, "c1")

Respond = Callable[[list[ModelMessage], AgentInfo], ModelResponse]


def scripted(respond: Respond, gate: asyncio.Event | None = None, gate_from: int = 0):
    """A model answering with `respond`, streamed as the agent asks. With
    `gate`, every request from the `gate_from`-th (0-based) waits for it."""
    requests = 0

    async def call(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal requests
        requests += 1
        if gate is not None and requests > gate_from:
            await gate.wait()
        return respond(messages, info)

    async def stream(messages: list[ModelMessage], info: AgentInfo):
        for index, part in enumerate((await call(messages, info)).parts):
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {
                    index: DeltaToolCall(
                        name=part.tool_name,
                        json_args=part.args_as_json_str(),
                        tool_call_id=part.tool_call_id,
                    )
                }

    return FunctionModel(call, stream_function=stream)


def tool_then_text(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    """Calls READ_FILE, then answers with what came back."""
    for part in messages[-1].parts:
        if isinstance(part, ToolReturnPart) and part.tool_call_id == "c1":
            return ModelResponse(parts=[TextPart(f"saw: {part.content}")])
    return ModelResponse(parts=[READ_FILE])


def text(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("ok")])


def recording(seen: list, pick: Callable[[list[ModelMessage]], object]) -> Respond:
    """Answers "ok", keeping `pick(messages)` of each request."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(pick(messages))
        return text(messages, info)

    return respond


def last_prompt(messages: list[ModelMessage]) -> str:
    return messages[-1].parts[-1].content


@pytest.fixture
async def mcp():
    async with serve() as state:
        state.results.update(policy_list_files=FILES, policy_graph=GRAPH)
        yield state


@asynccontextmanager
async def platform(settings, mcp: FakeMCP, key, model: FunctionModel | None):
    """The app over its lifespan: a client with a valid token, and the
    shared turn state."""
    app = create_app(replace(settings, mcp_url=mcp.url), model=model)
    async with app.router.lifespan_context(app):
        keys = httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
        app.state.verifier = JwksTokenVerifier(
            keys=JwksCache(JWKS_URL, httpx.AsyncClient(transport=keys)),
            issuer=settings.issuer,
            audience=settings.resource_url,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://a"
        ) as client:
            client.headers["Authorization"] = f"Bearer {key.mint()}"
            yield client, app.state.turns


async def post(client: httpx.AsyncClient, **body) -> httpx.Response:
    return await client.post(TURNS, json={"session_id": None, "message": "hi", **body})


async def settle(turns: runner.Turns) -> None:
    """Wait for every turn task, as a detached browser would not."""
    if turns.running:
        await asyncio.wait(list(turns.running), timeout=10)


async def turn(client: httpx.AsyncClient, turns: runner.Turns, **body) -> list[dict]:
    """One whole turn: its events, once its task has ended."""
    response = await post(client, **body)
    await settle(turns)
    assert response.status_code == 200, response.text
    return parse(response.text)


async def run_turn(settings, mcp, key, respond: Respond, **body) -> list[dict]:
    async with platform(settings, mcp, key, scripted(respond)) as (client, turns):
        return await turn(client, turns, **body)


def parse(body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.split("\n")
        if line.startswith("data: ")
    ]


def kinds(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


async def rows(db, model) -> list:
    async with db() as session:
        return list(await session.scalars(select(model)))


# ——— The route ———


async def test_post_turn_happy_path(settings, mcp, key, db):
    seen: list[list[ModelMessage]] = []

    def respond(messages, info):
        seen.append(messages)
        return tool_then_text(messages, info)

    events = await run_turn(
        settings, mcp, key, respond, editor={"open_file": "a.yaml", "unsaved": []}
    )

    assert kinds(events) == [
        "RUN_STARTED",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    session_id = events[0]["threadId"]
    assert events[-1]["threadId"] == session_id
    assert events[1]["toolCallName"] == "policy_read_file"
    assert events[4]["content"] == "policy_read_file result"
    assert events[6]["delta"] == "saw: policy_read_file result"
    # The user message carries the project as the turn found it.
    prompt = last_prompt(seen[0])
    assert "Files: caps/refund.yaml, policy.yaml" in prompt
    assert "Open file: a.yaml" in prompt
    assert prompt.endswith("<message>\nhi\n</message>")
    (stored,) = await rows(db, AssistantMessage)
    assert str(stored.session_id) == session_id
    # The instructions go with every request, never into the history.
    assert all(m.get("instructions") is None for m in stored.content)
    assert await rows(db, AssistantTurnLock) == []
    (usage,) = await rows(db, AssistantUsage)
    assert (usage.org_id, usage.user_id) == (ORG, SUB)
    assert usage.input_tokens > 0


async def test_when_a_session_goes_on_then_the_model_gets_its_history(
    settings, mcp, key
):
    seen: list[list[ModelMessage]] = []
    model = scripted(recording(seen, lambda m: m))

    async with platform(settings, mcp, key, model) as (client, turns):
        first = await turn(client, turns, message="one")
        await turn(client, turns, session_id=first[0]["threadId"], message="two")

    assert len(seen[1]) == 3
    assert seen[1][1].parts[0].content == "ok"


async def test_when_no_model_is_configured_then_503(settings, mcp, key):
    async with platform(settings, mcp, key, None) as (client, _):
        response = await post(client)

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "disabled"}}


async def test_when_the_token_is_missing_then_401_before_anything(settings, mcp, key):
    async with platform(settings, mcp, key, scripted(text)) as (client, _):
        del client.headers["Authorization"]
        response = await post(client)

    assert response.status_code == 401
    assert mcp.calls == []


async def test_when_the_project_is_not_the_tokens_then_403(settings, mcp, key):
    async with platform(settings, mcp, key, scripted(text)) as (client, _):
        response = await client.post(
            f"/v1/assistant/projects/{OTHER_PROJECT}/turns",
            json={"session_id": None, "message": "hi"},
        )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"message": "x" * 8001}, id="message-too-long"),
        pytest.param({"message": ""}, id="message-empty"),
        pytest.param(
            {"editor": {"open_file": None, "unsaved": ["f"] * 51}}, id="unsaved"
        ),
        pytest.param({"session_id": "not-a-uuid"}, id="session-id"),
        pytest.param({"extra": 1}, id="unknown-field"),
    ],
)
async def test_when_the_body_is_invalid_then_422_and_no_lock(
    settings, mcp, key, db, body
):
    async with platform(settings, mcp, key, scripted(text)) as (client, _):
        response = await post(client, **body)

    assert response.status_code == 422
    assert await rows(db, AssistantTurnLock) == []


async def test_when_a_turn_is_running_then_a_second_one_is_409_busy(
    settings, mcp, key, db
):
    gate = asyncio.Event()
    async with platform(settings, mcp, key, scripted(text, gate)) as (client, turns):
        first = asyncio.create_task(post(client))
        while not await rows(db, AssistantTurnLock):
            await asyncio.sleep(0.01)
        second = await post(client)
        gate.set()
        await settle(turns)

    assert second.status_code == 409
    assert second.json() == {"detail": {"code": "busy"}}
    assert kinds(parse((await first).text))[-1] == "RUN_FINISHED"


async def test_when_the_session_is_another_users_then_404(settings, mcp, key, db):
    other = await SessionStore(db).open(
        user_id="someone-else",
        project_id=PROJECT,
        session_id=None,
        title="t",
        prompt_version="v",
    )
    async with platform(settings, mcp, key, scripted(text)) as (client, _):
        response = await post(client, session_id=str(other.id))

    assert response.status_code == 404
    assert await rows(db, AssistantTurnLock) == []


async def test_when_mcp_cannot_be_reached_then_502_and_the_lock_is_released(
    settings, mcp, key, db
):
    async with platform(settings, mcp, key, scripted(text)) as (client, turns):
        turns.mcp_url = "http://127.0.0.1:9/mcp"
        response = await post(client)

    assert response.status_code == 502
    assert await rows(db, AssistantTurnLock) == []
    # Refused before a session was made: no empty chat is left behind.
    assert await rows(db, AssistantSession) == []


# ——— The stream ———


async def test_when_the_turn_is_silent_then_a_ping_goes_out(
    settings, mcp, key, db, monkeypatch
):
    assert runner.PING_SECONDS == 15
    monkeypatch.setattr(runner, "PING_SECONDS", 0.05)
    gate = asyncio.Event()

    async with platform(settings, mcp, key, scripted(text, gate)) as (client, turns):
        response = asyncio.create_task(post(client))
        # Silent once the turn-start reads are done and its session exists.
        while not await rows(db, AssistantSession):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        gate.set()
        response = await response
        await settle(turns)

    assert ": ping\n\n" in response.text
    assert kinds(parse(response.text))[-1] == "RUN_FINISHED"


async def test_when_the_browser_disconnects_then_the_turn_is_stored_and_unlocked(
    settings, mcp, key, db
):
    # The answer after the tool call waits until the reader has gone.
    gate = asyncio.Event()
    model = scripted(tool_then_text, gate, gate_from=1)
    token = AssistantToken(SUB, ORG, (PROJECT,), (), 0, token=key.mint())

    async with platform(settings, mcp, key, model) as (_, turns):
        request = runner.TurnRequest(None, "hi", Editor(None, ()))
        queue = await runner.start(turns, token, PROJECT, request)
        result_seen = asyncio.Event()

        async def read() -> None:
            async for chunk in runner.stream(queue):
                if '"TOOL_CALL_RESULT"' in chunk:
                    result_seen.set()

        reader = asyncio.create_task(read())
        await result_seen.wait()
        # As Starlette does on a disconnect: the response is cancelled.
        reader.cancel()
        gate.set()
        await settle(turns)

    history = [m for r in await rows(db, AssistantMessage) for m in r.content]
    assert history[-1]["parts"][0]["content"] == "saw: policy_read_file result"
    assert await rows(db, AssistantTurnLock) == []
    assert len(await rows(db, AssistantUsage)) == 1


# ——— How a turn ends ———


async def test_when_the_turn_runs_past_its_deadline_then_timeout_and_the_next_turn_repairs(
    settings, mcp, key, db, monkeypatch
):
    monkeypatch.setattr(runner, "TURN_DEADLINE", runner.TURN_DEADLINE / 150)  # 2 s
    mcp.delays["policy_read_file"] = 10
    seen: list[list[ModelMessage]] = []

    def respond(messages, info):
        seen.append(messages)
        return tool_then_text(messages, info)

    async with platform(settings, mcp, key, scripted(respond)) as (client, turns):
        first = await turn(client, turns)
        monkeypatch.setattr(runner, "TURN_DEADLINE", runner.TURN_DEADLINE * 150)
        mcp.delays.clear()
        await turn(client, turns, session_id=first[0]["threadId"], message="again")

    assert first[-1] == {
        "type": "RUN_ERROR",
        "message": "The answer took too long and was stopped.",
        "code": "timeout",
    }
    # The call the deadline cut has a result before the model sees it again.
    repaired = [
        p
        for m in seen[-1]
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_call_id == "c1"
    ]
    assert [p.outcome for p in repaired] == ["interrupted"]
    assert await rows(db, AssistantTurnLock) == []


async def test_when_a_tool_runs_over_its_time_then_the_model_is_told_and_the_turn_goes_on(
    settings, mcp, key, monkeypatch
):
    # The agent's MCP calls only; the turn-start reads keep their own limit.
    monkeypatch.setattr(agent_module, "MCP_CALL_SECONDS", 1)
    mcp.delays["policy_read_file"] = 3

    events = await run_turn(settings, mcp, key, tool_then_text)

    assert kinds(events)[-1] == "RUN_FINISHED"
    answer = "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert "timed out" in answer


async def test_when_a_tool_result_is_200_kib_then_the_model_gets_8000_tokens_with_both_ends(
    settings, mcp, key
):
    mcp.results["policy_read_file"] = "HEAD" + "x" * 200 * 1024 + "TAIL"
    seen: list[str] = []

    def respond(messages, info):
        seen.extend(
            p.model_response_str()
            for p in messages[-1].parts
            if isinstance(p, ToolReturnPart)
        )
        return tool_then_text(messages, info)

    await run_turn(settings, mcp, key, respond)

    assert estimate_tokens(seen[0]) <= 8000
    assert seen[0].startswith("HEAD") and seen[0].endswith("TAIL")
    assert "narrower request" in seen[0]


async def test_when_the_history_is_over_budget_then_context_full_and_nothing_stored(
    settings, mcp, key, db
):
    events = await run_turn(replace(settings, context_tokens=1000), mcp, key, text)

    assert kinds(events) == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1]["code"] == "context_full"
    assert events[-1]["message"] == "This conversation is full; start a new chat."
    assert await rows(db, AssistantMessage) == []
    assert await rows(db, AssistantTurnLock) == []


async def test_when_a_tool_result_fills_the_context_then_context_full_and_the_turn_is_stored(
    settings, mcp, key, db
):
    mcp.results["policy_read_file"] = "y" * 40_000
    # Room for the prefix and the message, not for the tool result.
    small = replace(settings, context_tokens=8_000)

    events = await run_turn(small, mcp, key, tool_then_text)

    assert events[-1]["code"] == "context_full"
    assert "TOOL_CALL_RESULT" in kinds(events)
    assert len(await rows(db, AssistantMessage)) == 1


def fail(_messages, _info):
    raise ModelHTTPError(500, "m", body="down")


def window_exceeded(_messages, _info):
    raise ModelContextWindowExceededError("m", "prompt is too long")


def never_stop(_messages, _info):
    return ModelResponse(
        parts=[ToolCallPart("policy_list_files", {"project_id": PROJECT})]
    )


@pytest.mark.parametrize(
    ("respond", "code"),
    [
        (fail, "provider"),
        (never_stop, "limit_reached"),
        (window_exceeded, "context_full"),
    ],
)
async def test_when_the_run_fails_then_run_error_names_why(
    settings, mcp, key, respond, code
):
    events = await run_turn(settings, mcp, key, respond)

    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == code


# ——— Turn-start reads ———


async def test_when_the_token_may_apply_then_the_card_says_this_turn_may_write(
    settings, mcp, key
):
    seen: list[str] = []
    model = scripted(recording(seen, last_prompt))

    async with platform(settings, mcp, key, model) as (client, turns):
        client.headers["Authorization"] = (
            f"Bearer {key.mint(scope='policy:read policy:apply')}"
        )
        await turn(client, turns)
        client.headers["Authorization"] = f"Bearer {key.mint()}"
        await turn(client, turns)

    assert "This turn may write: yes" in seen[0]
    assert "This turn may write: no" in seen[1]


async def test_when_turns_run_then_the_reference_is_read_once_and_given_as_instructions(
    settings, mcp, key
):
    seen: list[str | None] = []
    model = scripted(recording(seen, lambda m: m[-1].instructions))

    async with platform(settings, mcp, key, model) as (client, turns):
        await turn(client, turns)
        await turn(client, turns)
        bearer = client.headers["Authorization"]

    reads = sorted(
        json.dumps(c.arguments) for c in mcp.calls if c.tool == "policy_reference"
    )
    assert reads == sorted(
        ["{}", '{"section": "writing-conditions"}', '{"section": "yaml-shape"}']
    )
    assert all(s is not None and "policy_reference result" in s for s in seen)
    # Every read goes out with the user's own token.
    assert {c.authorization for c in mcp.calls} == {bearer}


async def test_when_the_reference_cannot_be_read_then_502_and_the_next_turn_reads_it(
    settings, mcp, key
):
    mcp.refusal = "down"
    async with platform(settings, mcp, key, scripted(text)) as (client, turns):
        refused = await post(client)
        mcp.refusal = None
        accepted = await post(client)
        await settle(turns)

    assert refused.status_code == 502
    assert accepted.status_code == 200
    assert turns.reference is not None


async def test_when_a_change_was_applied_earlier_then_the_card_names_it(
    settings, mcp, key, db
):
    store = SessionStore(db)
    session = await store.open(
        user_id=SUB, project_id=PROJECT, session_id=None, title="t", prompt_version="v"
    )
    change = {
        "change_id": "chg_1",
        "summary": "lower the cap",
        "files": [
            {"name": "caps/refund.yaml", "before_hash": "h0", "after_hash": "h1"}
        ],
        "checks": [{"id": "yaml", "status": "pass"}],
    }
    await store.append_event(
        session, {"type": "CUSTOM", "name": "change", "value": change}
    )
    seen: list[str] = []

    await run_turn(
        settings, mcp, key, recording(seen, last_prompt), session_id=str(session.id)
    )

    assert "Change: chg_1" in seen[0]
    assert "caps/refund.yaml: h0" in seen[0]


async def test_when_the_server_stops_mid_turn_then_the_turn_is_stored_and_unlocked(
    settings, mcp, key, db
):
    # The answer after the tool call never comes: the stop cancels the turn.
    model = scripted(tool_then_text, asyncio.Event(), gate_from=1)

    async with platform(settings, mcp, key, model) as (client, turns):
        response = asyncio.create_task(post(client))
        while not any(c.tool == "policy_read_file" for c in mcp.calls):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        # As the app's shutdown does, once.
        for task in list(turns.running):
            task.cancel()
        await settle(turns)
        response.cancel()

    history = [m for r in await rows(db, AssistantMessage) for m in r.content]
    stored = [
        (p["part_kind"], p.get("tool_call_id")) for m in history for p in m["parts"]
    ]
    assert ("tool-call", "c1") in stored
    assert ("tool-return", "c1") in stored
    assert await rows(db, AssistantTurnLock) == []


def test_shutdown_happy_path():
    # Streams cut, then turns stored and unlocked, inside Docker's 10 s.
    assert GRACEFUL_SECONDS + SHUTDOWN_SECONDS < 10
