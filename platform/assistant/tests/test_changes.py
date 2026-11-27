"""apply_change through a whole turn: the route, the agent and a fake MCP
server whose `policy_apply` answers as the platform's does."""

import asyncio
import json

import pytest
from fastmcp.exceptions import ToolError
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    ERROR_OUTCOMES,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from hexgate_assistant import changes, runner
from hexgate_assistant.agent import INSTRUCTIONS
from hexgate_assistant.changes import (
    AFTER_SILENCE,
    ATTEMPT_LIMIT,
    CONFLICT,
    NO_ANSWER,
    NO_REPLY,
    NO_SCOPE,
    NO_TIME,
    NOT_SENT,
    READ_FIRST,
    STALE,
    TOO_LARGE_TO_EDIT,
    UNEXPLAINED,
    UNREADABLE,
    Read,
    reads_in,
    redact,
    redact_calls,
    redact_retry,
)
from hexgate_assistant.context import definition_tokens, estimate_tokens
from hexgate_assistant.deps import Applied
from hexgate_assistant.models import AssistantMessage
from tests.fake_mcp import serve
from tests.test_runner import (
    FILES as LISTED,
)
from tests.test_runner import (
    GRAPH,
    PROJECT,
    TURN_TEST_SECONDS,
    TURNS,
    kinds,
    platform,
    rows,
    scripted,
    turn,
)

BODY = {"session_id": None, "message": "hi"}

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
    assert text == f"saw: {NO_REPLY}. {AFTER_SILENCE}"


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


async def test_when_the_server_stops_during_the_apply_then_the_stored_row_has_no_files(
    settings, mcp, key, db
):
    # The shutdown cancels the turn while the apply runs: no request follows
    # it, so only the store's own pass can take the file text out.
    mcp.delays["policy_apply"] = 30
    async with platform(settings, mcp, key, scripted(applying())) as (client, turns):
        client.headers["Authorization"] = f"Bearer {key.mint(scope=APPLY_SCOPE_TOKEN)}"
        response = asyncio.create_task(client.post(TURNS, json=BODY))
        async with asyncio.timeout(TURN_TEST_SECONDS):
            while not apply_calls(mcp):
                await asyncio.sleep(0.05)
    # Leaving the block ran the app's shutdown.
    response.cancel()

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
    assert json.loads(events[start + 1]["delta"]) == REDACTED


async def stored_text(db) -> str:
    return json.dumps([r.content for r in await rows(db, AssistantMessage)])


@pytest.mark.parametrize(
    "call",
    [
        # Imitating a redacted call, file text included.
        ToolCallPart("apply_change", {**ARGS, "change_id": None}, "a1"),
        # A name close to the tool's: refused as unknown, its files carried.
        ToolCallPart("apply_changes", ARGS, "a1"),
    ],
)
async def test_when_a_call_carries_files_under_any_shape_then_they_go_nowhere(
    settings, mcp, key, db, call
):
    events = await apply_turn(settings, mcp, key, script(READ, call))

    assert SENTINEL not in json.dumps(events)
    assert SENTINEL not in await stored_text(db)


async def test_when_the_tool_name_arrives_in_pieces_then_the_browser_gets_names_only(
    settings, mcp, key, db
):
    mcp.results["policy_apply"] = APPLIED
    requests = 0

    async def stream(messages: list[ModelMessage], _info: AgentInfo):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {
                0: DeltaToolCall(
                    name="policy_read_file",
                    json_args=READ.args_as_json_str(),
                    tool_call_id="r0",
                )
            }
        elif requests == 2:
            yield {0: DeltaToolCall(name="apply", tool_call_id="a1")}
            yield {0: DeltaToolCall(name="_change", json_args=json.dumps(ARGS))}
        else:
            yield "done"

    async with platform(settings, mcp, key, FunctionModel(stream_function=stream)) as (
        client,
        turns,
    ):
        client.headers["Authorization"] = f"Bearer {key.mint(scope=APPLY_SCOPE_TOKEN)}"
        events = await turn(client, turns)

    assert len(apply_calls(mcp)) == 1
    assert SENTINEL not in json.dumps(events)
    assert SENTINEL not in await stored_text(db)
    shown = [json.loads(e["delta"]) for e in events if e["type"] == "TOOL_CALL_ARGS"]
    assert shown[-1] == {"change_id": None, "files": NAMES, "summary": ARGS["summary"]}


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


NAMES = ["caps/refund.yaml", "caps/new.yaml"]
REDACTED = {"change_id": None, "files": NAMES, "summary": "lower the refund cap"}


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("apply_change", ARGS, REDACTED),
        # Imitating a redacted call: its change_id is never kept.
        ("apply_change", {**ARGS, "change_id": "chg_fake"}, REDACTED),
        # Another name, the same shape.
        ("apply_changes", ARGS, {"files": NAMES, "summary": "lower the refund cap"}),
        # A name with a space is still a name.
        (
            "policy_check",
            {"drafts": [{"name": "caps/refund rules.yaml", "content": "x"}]},
            {"drafts": ["caps/refund rules.yaml"]},
        ),
        (
            "policy_check",
            {"project_id": "p", "drafts": FILES},
            {"project_id": "p", "drafts": NAMES},
        ),
        # Cut short: whole files, for all anyone can tell.
        (
            "apply_change",
            {"INVALID_JSON": '{"files": [{"content": "x'},
            {**REDACTED, "files": [], "summary": ""},
        ),
    ],
)
def test_redact_happy_path(tool, args, expected):
    assert redact(tool, args) == expected


def test_when_no_file_text_is_carried_then_redact_keeps_the_arguments():
    args = {"project_id": "p", "name": "x"}

    assert redact("policy_read_file", args) is args


def test_when_a_redacted_call_is_redacted_again_then_only_our_change_id_stays():
    once = {"change_id": "chg_1", "files": NAMES, "summary": "s"}

    assert redact("apply_change", once) == {**once, "change_id": None}
    assert redact("apply_change", once, "chg_1") == once


def test_redact_calls_happy_path():
    applied = ModelResponse(parts=[ToolCallPart("apply_change", ARGS, "a1")])
    # Still running when the turn ended: no result, contents gone all the same.
    unanswered = ModelResponse(
        parts=[ToolCallPart("apply_change", json.dumps(ARGS), "a2")]
    )
    read = ModelResponse(parts=[ToolCallPart("policy_read_file", {"name": "x"}, "r")])

    once = redact_calls([applied, unanswered, read], Applied("chg_1", "a1"))
    # A later turn: stored history keeps the id we stored; the same call in
    # this turn's messages (the model's own) cannot carry one.
    later = redact_calls(once, None, stored=len(once))
    current = redact_calls(once, None)

    assert [p.args for m in later for p in m.parts] == [
        {**REDACTED, "change_id": "chg_1"},
        REDACTED,
        {"name": "x"},
    ]
    assert current[0].parts[0].args == REDACTED
    assert later[2] is read


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


async def test_when_a_file_is_saved_mid_turn_then_the_apply_after_the_read_is_stale(
    settings, mcp, key
):
    # Saved after the turn began: this turn's read differs from the snapshot.
    mcp.results["policy_read_file"] = read_result("h-later")

    events = await apply_turn(
        settings, mcp, key, script(READ, apply_call(1), apply_call(2))
    )

    assert apply_calls(mcp) == []
    # Writing ended: the second call gets the same answer, still no call.
    assert answer(events) == f"saw: {STALE.format(files='caps/refund.yaml')}"


async def test_when_a_file_is_too_large_to_read_then_it_is_too_large_to_edit(
    settings, mcp, key
):
    mcp.results["policy_read_file"] = json.dumps(
        {"name": "caps/refund.yaml", "content": "x" * 70_000, "text_hash": "h-refund"}
    )

    events = await apply_turn(settings, mcp, key, applying())

    assert apply_calls(mcp) == []
    assert (
        answer(events) == f"saw: {TOO_LARGE_TO_EDIT.format(files='caps/refund.yaml')}"
    )


async def test_when_too_little_of_the_turn_is_left_then_the_apply_does_not_start(
    settings, mcp, key, monkeypatch
):
    # 30 s in all: less than an apply may take (APPLY_START_SECONDS).
    monkeypatch.setattr(runner, "TURN_DEADLINE", runner.TURN_DEADLINE / 10)

    events = await apply_turn(settings, mcp, key, applying())

    assert apply_calls(mcp) == []
    assert answer(events) == f"saw: {NO_TIME}"


def test_reads_in_happy_path():
    def read(call_id: str, args: dict, result: str) -> list:
        return [
            ModelResponse(parts=[ToolCallPart("policy_read_file", args, call_id)]),
            ModelRequest(parts=[ToolReturnPart("policy_read_file", result, call_id)]),
        ]

    messages = [
        *read("r1", {"name": "caps/refund.yaml"}, read_result("h1")),
        *read("r2", {"name": "big.yaml"}, "file too large for the assistant (…)"),
        # A module (it has a tier) and a refusal: not reads of a file.
        *read("r3", {"name": "m.yaml", "tier": "boundary"}, read_result("hm")),
        *read("r4", {"name": "gone.yaml"}, "no policy file 'gone.yaml'"),
        # This turn starts here: a later read of the same file wins.
        *read("r5", {"name": "caps/refund.yaml"}, read_result("h2")),
    ]

    assert reads_in(messages, turn_start=8) == {
        "caps/refund.yaml": Read("h2", this_turn=True),
        "big.yaml": Read(None, this_turn=False),
    }


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


async def test_when_an_applied_answer_cannot_be_read_then_the_change_may_be_saved(
    settings, mcp, key
):
    mcp.results["policy_apply"] = json.dumps({"applied": True})

    events = await apply_turn(settings, mcp, key, applying(times=2))

    assert len(apply_calls(mcp)) == 1
    assert change_events(events) == []
    assert answer(events) == f"saw: {UNREADABLE}. {AFTER_SILENCE}"


async def test_when_failed_checks_run_long_then_the_cut_offers_no_narrower_call(
    settings, mcp, key
):
    detail = "x" * 40_000
    mcp.results["policy_apply"] = FAILED.replace('"bad"', json.dumps(detail))

    events = await apply_turn(settings, mcp, key, applying())

    text = answer(events)
    assert "bytes of check details cut" in text
    assert "narrower" not in text


CONTENT = FILES[0]["content"]


@pytest.mark.parametrize(
    "args",
    [
        {"files": json.dumps(FILES), "summary": "s"},
        {"files": FILES[0], "summary": "s"},
        {"files": {"caps/refund.yaml": CONTENT}, "summary": "s"},
        {"files": [f["content"] for f in FILES], "summary": "s"},
        {"files": [{"name": "caps/refund.yaml", "contnet": CONTENT}], "summary": "s"},
        # Encoded twice: a JSON string of the arguments.
        json.dumps(json.dumps(ARGS)),
        # Cut short, files as a string and as a list.
        '{"files": "' + CONTENT.replace("\n", "\\n"),
        '{"files": [{"name": "caps/refund.yaml", "content": "' + CONTENT[:12],
    ],
)
async def test_when_apply_change_gets_files_in_any_shape_then_their_text_goes_nowhere(
    settings, mcp, key, db, args
):
    seen: list[list[ModelMessage]] = []
    respond = script(READ, ToolCallPart("apply_change", args, "a1"))

    def recording(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return respond(messages, info)

    events = await apply_turn(settings, mcp, key, recording)

    assert apply_calls(mcp) == []
    assert SENTINEL not in json.dumps(events)
    assert SENTINEL not in await stored_text(db)
    assert SENTINEL not in repr(seen[-1])


def test_when_a_retry_quotes_the_refused_value_then_the_quote_is_cut():
    text = (
        "1 validation error for apply_change\nfiles\n  Input should be a valid "
        f"list [type=list_type, input_value='{CONTENT}', input_type=str]"
    )
    part = ToolReturnPart("apply_change", text, "a1", outcome=changes.RETRIED)

    assert SENTINEL not in redact_retry(part).content
    assert "input_value=…" in redact_retry(part).content


async def test_when_policy_apply_is_called_then_nothing_is_sent_after_it(
    settings, mcp, key
):
    mcp.results["policy_apply"] = APPLIED

    events = await apply_turn(settings, mcp, key, applying())

    assert len(change_events(events)) == 1
    # Listed before the call, so the client never lists after it.
    assert mcp.methods[-2:] == ["tools/list", "tools/call"]


async def test_when_the_call_cannot_be_sent_then_nothing_saved_and_writing_goes_on(
    settings, mcp, key
):
    mcp.results["policy_apply"] = APPLIED
    respond = applying(times=2)

    def flaky(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        response = respond(messages, info)
        call = response.parts[0]
        # The first apply's session cannot list the tools; the second can.
        mcp.refuse_listing = (
            isinstance(call, ToolCallPart) and call.tool_call_id == "a1"
        )
        return response

    events = await apply_turn(settings, mcp, key, flaky)

    assert len(apply_calls(mcp)) == 1
    assert len(change_events(events)) == 1
    results = [e["content"] for e in events if e["type"] == "TOOL_CALL_RESULT"]
    assert NOT_SENT in results


def test_retried_happy_path():
    # The outcome a retry is told apart by, as pydantic-ai names it.
    assert changes.RETRIED in ERROR_OUTCOMES


async def test_when_a_file_was_too_large_in_an_earlier_turn_then_it_is_read_again(
    settings, mcp, key
):
    mcp.results["policy_read_file"] = json.dumps(
        {"name": "caps/refund.yaml", "content": "x" * 70_000, "text_hash": "h-refund"}
    )
    model = script(READ, None, apply_call(1))
    async with platform(settings, mcp, key, scripted(model)) as (client, turns):
        client.headers["Authorization"] = f"Bearer {key.mint(scope=APPLY_SCOPE_TOKEN)}"
        first = await turn(client, turns)
        second = await turn(client, turns, session_id=first[0]["threadId"])

    assert apply_calls(mcp) == []
    assert answer(second) == f"saw: {READ_FIRST.format(files='caps/refund.yaml')}"


def stored_apply_args(rows_: list) -> list[dict]:
    return [
        p["args"]
        for r in rows_
        if r.kind == "messages"
        for m in r.content
        for p in m["parts"]
        if p.get("tool_name") == "apply_change" and p["part_kind"] == "tool-call"
    ]


async def test_when_a_change_is_applied_then_its_id_stays_in_the_history(
    settings, mcp, key, db
):
    mcp.results["policy_apply"] = APPLIED
    seen: list[list[ModelMessage]] = []
    respond = script(READ, apply_call(1), None)

    def recording(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return respond(messages, info)

    async with platform(settings, mcp, key, scripted(recording)) as (client, turns):
        client.headers["Authorization"] = f"Bearer {key.mint(scope=APPLY_SCOPE_TOKEN)}"
        first = await turn(client, turns)
        await turn(client, turns, session_id=first[0]["threadId"])

    # The turn's stored row, and the next turn's request.
    assert (
        stored_apply_args(await rows(db, AssistantMessage))[0]["change_id"] == "chg_1"
    )
    later = [
        p.args
        for m in seen[-1]
        for p in m.parts
        if isinstance(p, ToolCallPart) and p.tool_name == "apply_change"
    ]
    assert later == [{**REDACTED, "change_id": "chg_1"}]


async def test_when_a_call_brings_its_own_change_id_then_none_is_stored(
    settings, mcp, key, db
):
    fake = ToolCallPart(
        "apply_change", {"change_id": "chg_fake", "files": NAMES, "summary": "s"}, "a1"
    )

    await apply_turn(settings, mcp, key, script(fake))

    assert [
        a["change_id"] for a in stored_apply_args(await rows(db, AssistantMessage))
    ] == [None]


async def test_when_the_listing_is_slow_then_the_apply_is_not_sent_past_the_guard(
    settings, mcp, key, monkeypatch
):
    # The guard passes at the start (300 s left) but not after a 15 s listing.
    monkeypatch.setattr(changes, "APPLY_START_SECONDS", 290)
    respond = applying()

    def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        response = respond(messages, info)
        applies = getattr(response.parts[0], "tool_name", None) == "apply_change"
        mcp.list_delay = 15 if applies else 0
        return response

    events = await apply_turn(settings, mcp, key, slow)

    assert apply_calls(mcp) == []
    assert mcp.methods[-1] == "tools/list"
    assert answer(events) == f"saw: {NO_TIME}"


async def test_when_the_server_fails_unexplained_then_the_change_may_be_saved(
    settings, mcp, key
):
    mcp.errors["policy_apply"] = "500 boom"

    events = await apply_turn(settings, mcp, key, applying(times=2))

    assert len(apply_calls(mcp)) == 1
    assert answer(events) == f"saw: {UNEXPLAINED}: 500 boom. {AFTER_SILENCE}"


async def test_when_the_api_refuses_the_request_then_writing_stays_open(
    settings, mcp, key
):
    mcp.errors["policy_apply"] = '422 invalid request: [{"msg": "bad"}]'

    await apply_turn(settings, mcp, key, applying(times=2))

    assert len(apply_calls(mcp)) == 2


async def test_when_the_messages_cannot_be_stored_then_the_change_still_is(
    settings, mcp, key, db
):
    mcp.results["policy_apply"] = APPLIED
    async with platform(settings, mcp, key, scripted(applying())) as (client, turns):
        client.headers["Authorization"] = f"Bearer {key.mint(scope=APPLY_SCOPE_TOKEN)}"

        async def broken(*_args, **_kwargs):
            raise RuntimeError("database gone")

        turns.store.append_messages = broken
        await turn(client, turns)

    (event,) = [r for r in await rows(db, AssistantMessage) if r.kind == "event"]
    assert event.content["value"]["change_id"] == "chg_1"


async def test_when_the_session_fails_to_close_then_the_answer_stands(
    settings, mcp, key, monkeypatch
):
    mcp.results["policy_apply"] = APPLIED

    class FailingClose(MCPToolset):
        async def __aexit__(self, *args):
            result = await super().__aexit__(*args)
            # The session itself, not each call's nested use of it.
            if self._running_count == 0:
                raise RuntimeError("close failed")
            return result

    monkeypatch.setattr(changes, "MCPToolset", FailingClose)

    events = await apply_turn(settings, mcp, key, applying())

    assert len(change_events(events)) == 1
    assert NOT_SENT not in json.dumps(events)


async def test_when_the_call_is_never_sent_then_it_still_counts_as_an_attempt(
    settings, mcp, key
):
    respond = applying(times=4)

    def unreachable(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        response = respond(messages, info)
        part = response.parts[0]
        mcp.refuse_listing = getattr(part, "tool_name", None) == "apply_change"
        return response

    events = await apply_turn(settings, mcp, key, unreachable)

    assert apply_calls(mcp) == []
    assert answer(events) == f"saw: {ATTEMPT_LIMIT}"
