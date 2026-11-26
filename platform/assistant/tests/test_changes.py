"""apply_change through a whole turn: the route, the agent and a fake MCP
server whose `policy_apply` answers as the platform's does."""

import json

import pytest
from fastmcp.exceptions import ToolError
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo

from hexgate_assistant import changes, runner
from hexgate_assistant.agent import INSTRUCTIONS
from hexgate_assistant.changes import (
    AFTER_SILENCE,
    ATTEMPT_LIMIT,
    CONFLICT,
    NO_ANSWER,
    NO_REPLY,
    NO_SCOPE,
    READ_FIRST,
    call_summary,
    read_hash,
    redact_calls,
)
from hexgate_assistant.context import definition_tokens, estimate_tokens
from hexgate_assistant.models import AssistantMessage
from tests.fake_mcp import serve
from tests.test_runner import (
    FILES as LISTED,
)
from tests.test_runner import (
    GRAPH,
    PROJECT,
    kinds,
    platform,
    rows,
    scripted,
    turn,
)

# In every file the model writes: found nowhere it must not be.
SENTINEL = "SENTINEL-c0ffee-7d1f"
APPLY_SCOPE_TOKEN = "policy:read policy:apply"
FILES = [
    {"name": "caps/refund.yaml", "content": f"# {SENTINEL}\nrefund: {{}}\n"},
    {"name": "caps/new.yaml", "content": f"# {SENTINEL}\n"},
]
ARGS = {"files": FILES, "summary": "lower the refund cap"}
CHECKS = [
    {"id": "yaml", "status": "pass", "detail": None, "items": []},
    {"id": "dry_runs", "status": "pass", "detail": "2 samples", "items": []},
]
APPLIED = json.dumps(
    {
        "applied": True,
        "change_id": "chg_1",
        "checks": CHECKS,
        "boundary_changes": [],
        "files": [
            {"name": "caps/refund.yaml", "before_hash": "h-refund", "after_hash": "h2"},
            {"name": "caps/new.yaml", "before_hash": None, "after_hash": "h3"},
        ],
    }
)
FAILED = json.dumps(
    {
        "applied": False,
        "change_id": None,
        "checks": [{"id": "yaml", "status": "fail", "detail": "bad", "items": []}],
        "boundary_changes": [],
        "files": [],
    }
)
STALE_REFUSAL = '409 conflict: {"code":"stale","files":["caps/refund.yaml"]}'


def read_result(text_hash: str) -> str:
    """`policy_read_file`'s answer for caps/refund.yaml, read at `text_hash`."""
    return json.dumps(
        {
            "name": "caps/refund.yaml",
            "content": "refund: {}\n",
            "content_hash": "c",
            "text_hash": text_hash,
            "updated_at": "2026-11-26T00:00:00Z",
        }
    )


READ = ToolCallPart(
    "policy_read_file", {"project_id": PROJECT, "name": "caps/refund.yaml"}, "r0"
)


@pytest.fixture
async def mcp():
    async with serve() as state:
        state.results.update(
            policy_list_files=LISTED,
            policy_graph=GRAPH,
            policy_read_file=read_result("h-refund"),
        )
        yield state


def script(*steps: ToolCallPart | None):
    """A model that makes each call in turn, one per request; at a None, or
    once the calls run out, it answers with the last result, which ends the
    turn."""
    queue = list(steps)

    def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        step = queue.pop(0) if queue else None
        if step is not None:
            return ModelResponse(parts=[step])
        last = messages[-1].parts[-1]
        return ModelResponse(parts=[TextPart(f"saw: {last.content}")])

    return respond


def apply_call(n: int) -> ToolCallPart:
    return ToolCallPart("apply_change", ARGS, f"a{n}")


def applying(times: int = 1, read: bool = True):
    """A model that reads caps/refund.yaml (unless `read` is false), then
    calls apply_change `times` times."""
    return script(*([READ] if read else []), *map(apply_call, range(1, times + 1)))


async def apply_turn(settings, mcp, key, respond, scope=APPLY_SCOPE_TOKEN, **body):
    async with platform(settings, mcp, key, scripted(respond)) as (client, turns):
        client.headers["Authorization"] = f"Bearer {key.mint(scope=scope)}"
        return await turn(client, turns, **body)


def apply_calls(mcp) -> list[dict]:
    return [c.arguments for c in mcp.calls if c.tool == "policy_apply"]


def answer(events: list[dict]) -> str:
    return "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")


def change_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "CUSTOM"]


async def test_apply_change_happy_path(settings, mcp, key, db):
    mcp.results["policy_apply"] = APPLIED

    events = await apply_turn(settings, mcp, key, applying())

    # Base hashes from the turn-start snapshot, null for a new file; the
    # turn's project and session.
    (call,) = apply_calls(mcp)
    assert [(f["name"], f["base_hash"]) for f in call["files"]] == [
        ("caps/refund.yaml", "h-refund"),
        ("caps/new.yaml", None),
    ]
    assert call["project_id"] == PROJECT
    assert call["session_id"] == events[0]["threadId"]
    assert call["summary"] == "lower the refund cap"
    expected = {
        "change_id": "chg_1",
        "summary": "lower the refund cap",
        "files": json.loads(APPLIED)["files"],
        "checks": [
            {"id": "yaml", "status": "pass", "detail": None},
            {"id": "dry_runs", "status": "pass", "detail": "2 samples"},
        ],
    }
    assert change_events(events) == [
        {"type": "CUSTOM", "name": "change", "value": expected}
    ]
    stored = await rows(db, AssistantMessage)
    assert [r.kind for r in stored] == ["messages", "event"]
    assert stored[1].content["value"] == expected


async def test_when_the_token_has_no_apply_scope_then_refused_and_mcp_never_called(
    settings, mcp, key
):
    events = await apply_turn(settings, mcp, key, applying(), scope="policy:read")

    assert apply_calls(mcp) == []
    assert answer(events) == f"saw: {NO_SCOPE}"
    assert change_events(events) == []


async def test_when_checks_fail_then_the_model_gets_them_and_no_change_event(
    settings, mcp, key, db
):
    mcp.results["policy_apply"] = FAILED

    events = await apply_turn(settings, mcp, key, applying())

    assert json.loads(answer(events).removeprefix("saw: ")) == json.loads(FAILED)
    assert change_events(events) == []
    assert [r.kind for r in await rows(db, AssistantMessage)] == ["messages"]


async def test_when_a_fourth_attempt_is_made_then_attempt_limit_reached(
    settings, mcp, key
):
    mcp.results["policy_apply"] = FAILED

    events = await apply_turn(settings, mcp, key, applying(times=4))

    assert len(apply_calls(mcp)) == 3
    assert answer(events) == f"saw: {ATTEMPT_LIMIT}"


async def test_when_a_change_was_applied_then_a_second_apply_is_refused(
    settings, mcp, key
):
    mcp.results["policy_apply"] = APPLIED

    events = await apply_turn(settings, mcp, key, applying(times=2))

    assert len(apply_calls(mcp)) == 1
    assert "refused: change chg_1 was already applied this turn" in answer(events)
    assert len(change_events(events)) == 1


async def test_when_the_model_applies_twice_in_parallel_then_one_reaches_mcp(
    settings, mcp, key
):
    mcp.results["policy_apply"] = APPLIED
    results: list[str] = []

    def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        returns = [p for p in messages[-1].parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[READ])
        if returns[0].tool_name == "policy_read_file":
            return ModelResponse(
                parts=[ToolCallPart("apply_change", ARGS, f"p{i}") for i in (1, 2)]
            )
        results.extend(str(p.content) for p in returns)
        return ModelResponse(parts=[TextPart("done")])

    events = await apply_turn(settings, mcp, key, respond)

    assert len(apply_calls(mcp)) == 1
    assert len(change_events(events)) == 1
    assert sum("already applied" in r for r in results) == 1


async def test_when_a_file_is_stale_then_writing_ends_for_the_turn(settings, mcp, key):
    mcp.errors["policy_apply"] = STALE_REFUSAL

    events = await apply_turn(settings, mcp, key, applying(times=2))

    # The second call is refused here: it would send the same hashes.
    assert len(apply_calls(mcp)) == 1
    text = answer(events)
    assert "changed since this turn began: caps/refund.yaml" in text
    assert "send their message again" in text


async def test_when_the_platform_refuses_then_nothing_saved_and_the_model_may_retry(
    settings, mcp, key
):
    refusal = '403 forbidden: {"code":"assistant_mode"}'
    mcp.errors["policy_apply"] = refusal

    events = await apply_turn(settings, mcp, key, applying(times=2))

    assert len(apply_calls(mcp)) == 2
    assert answer(events) == f"saw: refused, nothing saved: {refusal}"


async def test_when_any_conflict_reaches_mcp_then_writing_ends_for_the_turn(
    settings, mcp, key
):
    refusal = '409 conflict: {"code":"layout"}'
    mcp.errors["policy_apply"] = refusal

    events = await apply_turn(settings, mcp, key, applying(times=2))

    assert len(apply_calls(mcp)) == 1
    assert answer(events) == f"saw: refused, nothing saved: {refusal}. {CONFLICT}"


async def test_when_the_api_does_not_answer_then_the_change_may_be_saved(
    settings, mcp, key, monkeypatch
):
    # The server's limit, scaled down to 1 s, runs out and it answers that
    # the API did not, while the API may still commit. Ours is above it, as
    # APPLY_CALL_SECONDS is above APPLY_TIMEOUT_SECONDS.
    monkeypatch.setattr(changes, "APPLY_CALL_SECONDS", 3)
    mcp.delays["policy_apply"] = 1
    mcp.errors["policy_apply"] = NO_ANSWER

    events = await apply_turn(settings, mcp, key, applying(times=2))

    assert len(apply_calls(mcp)) == 1
    assert answer(events) == f"saw: {NO_ANSWER}. {AFTER_SILENCE}"


async def test_when_our_limit_ends_first_then_the_server_text_never_reaches_the_model(
    settings, mcp, key, monkeypatch
):
    # Ours ends first, as it does at equal limits: the server's answer comes
    # after its own limit plus transit, so its "may be saved" never reaches
    # the model. The server has the call, the API may commit it, so
    # the model must still not read this as a failure it may retry.
    monkeypatch.setattr(changes, "APPLY_CALL_SECONDS", 1)
    mcp.delays["policy_apply"] = 1.5
    mcp.errors["policy_apply"] = NO_ANSWER

    events = await apply_turn(settings, mcp, key, applying(times=2))

    assert len(apply_calls(mcp)) == 1
    text = answer(events)
    assert NO_ANSWER not in text
    assert text == f"saw: {NO_REPLY} {AFTER_SILENCE}"


async def test_when_a_change_returns_then_no_later_request_or_stored_row_has_its_files(
    settings, mcp, key, db
):
    mcp.results["policy_apply"] = APPLIED
    seen: list[list[ModelMessage]] = []
    respond = applying()

    def recording(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return respond(messages, info)

    await apply_turn(settings, mcp, key, recording)

    # Requests: the first, after the read, after the apply.
    later = repr(seen[2])
    assert SENTINEL not in later
    call = next(
        p
        for m in seen[2]
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, ToolCallPart) and p.tool_name == "apply_change"
    )
    assert call.args == {
        "change_id": "chg_1",
        "files": ["caps/refund.yaml", "caps/new.yaml"],
        "summary": "lower the refund cap",
    }
    stored = json.dumps([r.content for r in await rows(db, AssistantMessage)])
    assert SENTINEL not in stored
    # The policy files reached MCP whole.
    assert SENTINEL in apply_calls(mcp)[0]["files"][0]["content"]


async def test_when_the_turn_ends_on_the_call_then_the_stored_row_has_no_files(
    settings, mcp, key, db, monkeypatch
):
    # The deadline cuts the apply while it runs: no request follows it, so
    # only the store's own pass can take the contents out.
    monkeypatch.setattr(runner, "TURN_DEADLINE", runner.TURN_DEADLINE / 30)  # 10 s
    mcp.delays["policy_apply"] = 30

    events = await apply_turn(settings, mcp, key, applying())

    assert events[-1]["code"] == "timeout"
    stored = [m for r in await rows(db, AssistantMessage) for m in r.content]
    calls = [
        p["args"]
        for m in stored
        for p in m["parts"]
        if p.get("tool_name") == "apply_change" and p["part_kind"] == "tool-call"
    ]
    assert calls == [
        {
            "change_id": None,
            "files": ["caps/refund.yaml", "caps/new.yaml"],
            "summary": "lower the refund cap",
        }
    ]
    assert SENTINEL not in json.dumps(stored)


async def test_when_apply_change_streams_then_the_browser_gets_names_only(
    settings, mcp, key
):
    mcp.results["policy_apply"] = APPLIED

    events = await apply_turn(settings, mcp, key, applying())

    assert SENTINEL not in json.dumps(events)
    start = next(
        i for i, e in enumerate(events) if e.get("toolCallName") == "apply_change"
    )
    assert kinds(events)[start : start + 5] == [
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "CUSTOM",
        "TOOL_CALL_RESULT",
    ]
    assert json.loads(events[start + 1]["delta"]) == {
        "files": ["caps/refund.yaml", "caps/new.yaml"],
        "summary": "lower the refund cap",
    }


async def test_when_a_change_was_applied_then_the_next_turn_names_it(
    settings, mcp, key
):
    mcp.results["policy_apply"] = APPLIED
    prompts: list[str] = []
    respond = applying()

    def recording(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(str(messages[-1].parts[-1].content))
        if len(prompts) > 3:
            return ModelResponse(parts=[TextPart("ok")])
        return respond(messages, info)

    async with platform(settings, mcp, key, scripted(recording)) as (client, turns):
        client.headers["Authorization"] = f"Bearer {key.mint(scope=APPLY_SCOPE_TOKEN)}"
        first = await turn(client, turns)
        await turn(client, turns, session_id=first[0]["threadId"])

    # Requests of turn 1: the first, after the read, after the apply.
    assert "Change: chg_1" in prompts[3]


async def test_when_turns_run_then_the_prefix_counts_apply_change(settings, mcp, key):
    offered: list = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.extend(info.function_tools)
        return ModelResponse(parts=[TextPart("ok")])

    async with platform(settings, mcp, key, scripted(respond)) as (client, turns):
        await turn(client, turns)

    assert "apply_change" in {t.name for t in offered}
    definitions = [(t.name, t.description, t.parameters_json_schema) for t in offered]
    assert turns.prefix_tokens >= estimate_tokens(
        INSTRUCTIONS + turns.reference
    ) + definition_tokens(definitions)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ARGS,
            {
                "files": ["caps/refund.yaml", "caps/new.yaml"],
                "summary": "lower the refund cap",
            },
        ),
        ({"INVALID_JSON": "{"}, {"files": [], "summary": ""}),
        ({"files": "x", "summary": 3}, {"files": [], "summary": ""}),
        ({"files": [{"content": "c"}, "y"]}, {"files": [], "summary": ""}),
    ],
)
def test_call_summary_happy_path(args, expected):
    assert call_summary(args) == expected


def test_redact_calls_happy_path():
    applied = ModelResponse(parts=[ToolCallPart("apply_change", ARGS, "a1")])
    # Still running when the turn ended: no result, contents gone all the same.
    unanswered = ModelResponse(
        parts=[ToolCallPart("apply_change", json.dumps(ARGS), "a2")]
    )
    read = ModelResponse(parts=[ToolCallPart("policy_read_file", {"name": "x"}, "r")])

    once = redact_calls([applied, unanswered, read], {"a1": "chg_1"})
    # A later request (or turn) without the id map keeps the first answer.
    twice = redact_calls(once, {})

    names = {"files": ["caps/refund.yaml", "caps/new.yaml"], "summary": ARGS["summary"]}
    assert [p.args for m in twice for p in m.parts] == [
        {"change_id": "chg_1", **names},
        {"change_id": None, **names},
        {"name": "x"},
    ]
    assert twice[2] is read


async def test_when_a_file_was_never_read_then_refused_without_mcp(settings, mcp, key):
    events = await apply_turn(settings, mcp, key, applying(read=False))

    assert apply_calls(mcp) == []
    assert answer(events) == f"saw: {READ_FIRST.format(files='caps/refund.yaml')}"


async def test_when_the_model_reads_after_a_refusal_then_the_same_turn_applies(
    settings, mcp, key
):
    mcp.results["policy_apply"] = APPLIED

    events = await apply_turn(
        settings, mcp, key, script(apply_call(1), READ, apply_call(2))
    )

    (call,) = apply_calls(mcp)
    assert call["files"][0]["base_hash"] == "h-refund"
    assert len(change_events(events)) == 1


async def test_when_the_admin_saves_between_turns_then_the_old_read_is_refused(
    settings, mcp, key
):
    # Turn 1 read the file at h-old; the admin saved it since: h-refund.
    mcp.results["policy_read_file"] = read_result("h-old")
    mcp.results["policy_apply"] = APPLIED
    model = script(READ, None, apply_call(1))
    async with platform(settings, mcp, key, scripted(model)) as (client, turns):
        client.headers["Authorization"] = f"Bearer {key.mint(scope=APPLY_SCOPE_TOKEN)}"
        first = await turn(client, turns)
        second = await turn(client, turns, session_id=first[0]["threadId"])

    assert apply_calls(mcp) == []
    assert answer(second) == f"saw: {READ_FIRST.format(files='caps/refund.yaml')}"


async def test_when_a_change_was_applied_then_the_next_turn_reads_again_first(
    settings, mcp, key
):
    mcp.results["policy_apply"] = APPLIED
    model = script(READ, apply_call(1), None, apply_call(2), READ, apply_call(3))
    async with platform(settings, mcp, key, scripted(model)) as (client, turns):
        client.headers["Authorization"] = f"Bearer {key.mint(scope=APPLY_SCOPE_TOKEN)}"
        first = await turn(client, turns)
        # The change saved caps/refund.yaml at h2: the next snapshot has it.
        mcp.results["policy_list_files"] = LISTED.replace("h-refund", "h2")
        mcp.results["policy_read_file"] = read_result("h2")
        second = await turn(client, turns, session_id=first[0]["threadId"])

    assert [c["files"][0]["base_hash"] for c in apply_calls(mcp)] == ["h-refund", "h2"]
    assert len(change_events(first)) == len(change_events(second)) == 1


async def test_when_a_file_is_saved_mid_turn_then_the_reads_stop_at_the_attempt_cap(
    settings, mcp, key
):
    # Saved after the turn began: every read now differs from the snapshot.
    mcp.results["policy_read_file"] = read_result("h-later")
    model = script(*[p for n in (1, 2, 3, 4) for p in (READ, apply_call(n))])

    events = await apply_turn(settings, mcp, key, model)

    assert apply_calls(mcp) == []
    assert answer(events) == f"saw: {ATTEMPT_LIMIT}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (read_result("h1"), ("caps/refund.yaml", "h1")),
        ("file too large for the assistant", None),
        (json.dumps({"name": "m", "text_hash": "h", "tier": "boundary"}), None),
        (json.dumps({"name": "x"}), None),
    ],
)
def test_read_hash_happy_path(text, expected):
    assert read_hash(text) == expected


async def test_when_the_api_refuses_the_files_then_their_contents_are_not_echoed(
    settings, mcp, key, db
):
    # FastAPI's 422 detail echoes each failing value, here a file's content.
    detail = [{"type": "too_long", "loc": ["body", "files"], "input": FILES}]
    mcp.errors["policy_apply"] = f"422 invalid request: {json.dumps(detail)}"

    events = await apply_turn(settings, mcp, key, applying())

    assert '"too_long"' in answer(events)
    assert SENTINEL not in json.dumps(events)
    stored = json.dumps([r.content for r in await rows(db, AssistantMessage)])
    assert SENTINEL not in stored


async def test_when_the_arguments_are_invalid_then_the_retry_does_not_echo_them(
    settings, mcp, key, db
):
    # No summary: the retry prompt's validation error carries the arguments.
    bad = ToolCallPart("apply_change", {"files": FILES}, "a1")
    seen: list[list[ModelMessage]] = []
    respond = script(READ, bad)

    def recording(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return respond(messages, info)

    events = await apply_turn(settings, mcp, key, recording)

    assert "summary" in repr(seen[-1][-1])
    assert SENTINEL not in repr(seen[-1])
    assert SENTINEL not in json.dumps(events)
    stored = json.dumps([r.content for r in await rows(db, AssistantMessage)])
    assert SENTINEL not in stored


def test_refusal_text_happy_path():
    refusal = ToolError("409 conflict: {}")

    assert changes._refusal_text(refusal) == "409 conflict: {}"
    # From the MCP session's task group.
    assert changes._refusal_text(ExceptionGroup("g", [refusal])) == "409 conflict: {}"
    assert changes._refusal_text(ExceptionGroup("g", [refusal, OSError()])) is None
    assert changes._refusal_text(TimeoutError()) is None
