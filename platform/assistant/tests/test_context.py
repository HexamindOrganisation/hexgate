import json
from datetime import date
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

from hexgate_assistant.context import (
    TRUNCATED_MARKER,
    WHOLE_RESULT_BYTES,
    WHOLE_RESULT_TOOLS,
    BudgetGuard,
    ContextFull,
    Editor,
    ProjectState,
    ProjectUnreadable,
    cap_tool_result,
    context_budget,
    context_tokens,
    editor_text,
    estimate_tokens,
    keep_ends,
    latest_change_text,
    project_card,
    read_project,
    user_message,
)


def test_keep_ends_happy_path():
    assert keep_ends("short", 100, "[{cut}]") == "short"


@pytest.mark.parametrize("filler", ["x", "é", "🙂"])
def test_when_text_is_over_the_cap_then_both_ends_are_kept_within_it(filler):
    text = "HEAD" + filler * 5000 + "TAIL"

    out = keep_ends(text, 1000, "[{cut} cut]")

    assert len(out.encode()) <= 1000
    assert out.startswith("HEAD") and out.endswith("TAIL")
    head, _, rest = out.partition("[")
    cut = int(rest.split(" ")[0])
    assert len(head.encode()) + cut + len(rest.split("]", 1)[1].encode()) == len(
        text.encode()
    )


def test_context_budget_happy_path():
    assert context_budget(None, 64_000) == 51_200


def test_when_the_model_window_is_smaller_then_it_sets_the_budget():
    assert context_budget(10_000, 64_000) == 8_000


def test_when_a_response_reported_usage_then_it_counts_instead_of_the_estimate():
    request = ModelRequest(parts=[UserPromptPart("x" * 3000)])
    response = ModelResponse(
        parts=[TextPart("ok")], usage=RequestUsage(input_tokens=50, output_tokens=5)
    )
    after = ModelRequest(parts=[UserPromptPart("y" * 300)])

    measured = context_tokens([request, response, after], prefix_tokens=10_000)

    assert 55 + 100 <= measured < 1000


def test_when_no_response_reported_usage_then_the_prefix_is_added():
    request = ModelRequest(parts=[UserPromptPart("x" * 3000)])

    assert context_tokens([request], prefix_tokens=500) > 1500


def test_when_the_history_is_over_the_budget_then_the_guard_raises():
    messages = [ModelRequest(parts=[UserPromptPart("x" * 3000)])]

    assert BudgetGuard(budget=10_000, prefix_tokens=0)(messages) is messages
    with pytest.raises(ContextFull):
        BudgetGuard(budget=500, prefix_tokens=0)(messages)


def test_estimate_tokens_happy_path():
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abcd") == 2


def project(**changes) -> ProjectState:
    values = {
        "snapshot": {"policy.yaml": "h1"},
        "layout": "compose",
        "roles": ("support",),
        "agents": ("billing",),
    }
    return ProjectState(**{**values, **changes})


def test_project_card_happy_path():
    card = project_card(project(), may_write=True, today=date(2026, 11, 25))

    assert card == (
        "Today: 2026-11-25\n"
        "This turn may write: yes\n"
        "Layout: compose\n"
        "Files: policy.yaml\n"
        "Roles: support\n"
        "Agents: billing"
    )


def test_when_a_project_has_many_roles_then_the_card_lists_50():
    roles = tuple(f"r{i:03}" for i in range(120))

    card = project_card(project(roles=roles), may_write=False, today=date.today())

    line = next(x for x in card.splitlines() if x.startswith("Roles:"))
    assert line.count(",") == 49
    assert line.endswith("(and 70 more)")


def test_when_no_change_was_applied_then_latest_change_is_none():
    assert latest_change_text(None) == "none"


def test_editor_text_happy_path():
    assert editor_text(Editor("a.yaml", ("a.yaml", "b.yaml"))) == (
        "Open file: a.yaml\nUnsaved edits in: a.yaml, b.yaml"
    )


def test_when_the_card_is_huge_then_the_user_message_still_ends_with_the_message():
    out = user_message(
        card="r, " * 10_000, latest_change="none", editor="none", message="hi"
    )

    assert estimate_tokens(out) < 3000
    assert out.endswith("<message>\nhi\n</message>")


def test_when_roles_could_not_be_read_then_the_card_says_unknown_not_none():
    card = project_card(
        project(roles=None, agents=None), may_write=False, today=date.today()
    )

    assert "Roles: unknown (not readable at turn start)" in card
    assert "Agents: unknown (not readable at turn start)" in card


def test_when_requests_carry_instructions_then_the_estimate_leaves_them_out():
    bare = [ModelRequest(parts=[UserPromptPart("hi")])]
    instructed = [ModelRequest(parts=[UserPromptPart("hi")], instructions="x" * 30_000)]

    assert context_tokens(instructed, 0) == context_tokens(bare, 0)


def test_cap_tool_result_happy_path():
    text = "HEAD" + "x" * 200 * 1024 + "TAIL"

    capped = cap_tool_result("audit_decisions", text)

    assert estimate_tokens(capped) <= 8000
    assert capped.startswith("HEAD") and capped.endswith("TAIL")
    assert "narrower request" in capped


@pytest.mark.parametrize("tool", sorted(WHOLE_RESULT_TOOLS))
def test_when_a_whole_result_tool_answers_under_64_kib_then_it_is_kept_whole(tool):
    text = "y" * (WHOLE_RESULT_BYTES - 10)

    assert cap_tool_result(tool, text) == text


def record(content: str) -> str:
    """A policy_read_file result, as the MCP server writes it."""
    return json.dumps(
        {
            "name": "caps/big.yaml",
            "content": content,
            "content_hash": "a" * 64,
            "text_hash": "b" * 64,
            "updated_at": "2026-11-25T10:00:00Z",
        },
        ensure_ascii=False,
    )


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("z" * WHOLE_RESULT_BYTES, id="ascii"),
        pytest.param("é" * (WHOLE_RESULT_BYTES // 2), id="two-byte"),
        # Every line's newline and quotes escaped: the record runs well past
        # 64 KiB while the file itself does not.
        pytest.param(('k: "v"\n' * 10000)[:WHOLE_RESULT_BYTES], id="escapes"),
    ],
)
def test_when_a_policy_file_is_at_64_kib_then_it_is_given_whole(content):
    assert len(content.encode()) == WHOLE_RESULT_BYTES

    assert cap_tool_result("policy_read_file", record(content)) == record(content)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("z" * (WHOLE_RESULT_BYTES + 1), id="ascii"),
        pytest.param("é" * (WHOLE_RESULT_BYTES // 2) + "z", id="two-byte"),
    ],
)
def test_when_a_policy_file_is_over_64_kib_then_none_of_it_is_given(content):
    capped = cap_tool_result("policy_read_file", record(content))

    assert capped == (
        "file too large for the assistant (over 64 KiB: 64 KiB); edit it in the editor"
    )


def test_when_a_policy_file_result_was_cut_by_the_server_then_it_is_refused():
    cut = record("z" * 300 * 1024)[: 256 * 1024] + TRUNCATED_MARKER

    assert cap_tool_result("policy_read_file", cut).startswith("file too large")


class Listing:
    """An MCP session answering the turn-start reads with fixed texts."""

    def __init__(self, files: str) -> None:
        self.files = files

    async def direct_call_tool(self, tool: str, args: dict) -> str:
        if tool == "policy_list_files":
            return self.files
        return json.dumps({"roles": [], "declared_agents": []})


async def test_when_the_file_list_was_cut_by_the_server_then_the_project_is_unreadable():
    whole = json.dumps(
        {"files": [{"name": f"f{i}", "text_hash": "h"} for i in range(9)]}
    )
    cut = whole[:60] + TRUNCATED_MARKER

    with pytest.raises(ProjectUnreadable, match="more policy files and modules"):
        await read_project(Listing(cut), "p1")


async def test_read_project_happy_path():
    files = json.dumps(
        {"files": [{"name": "policy.yaml", "text_hash": "h1"}], "modules": []}
    )

    project = await read_project(Listing(files), "p1")

    assert project.snapshot == {"policy.yaml": "h1"}
    assert project.layout == "compose"


def test_when_a_reference_section_is_over_64_kib_then_it_is_cut_as_any_result():
    capped = cap_tool_result("policy_reference", "r" * (WHOLE_RESULT_BYTES + 1))

    assert estimate_tokens(capped) <= 8000


DOCS = Path(__file__).resolve().parents[3] / "docs" / "policy"


def test_every_policy_reference_section_fits_in_a_whole_result():
    sections = sorted(DOCS.glob("*.mdx"))

    assert sections, DOCS
    too_big = [s.name for s in sections if len(s.read_bytes()) > WHOLE_RESULT_BYTES]
    assert too_big == []


def test_when_every_name_list_is_long_then_each_line_keeps_whole_names():
    def names(kind: str) -> tuple[str, ...]:
        return tuple(f"{kind}-{i:03}-" + "n" * (28 - len(kind) - 6) for i in range(60))

    files, roles, agents = names("file"), names("role"), names("agent")
    card = project_card(
        project(snapshot=dict.fromkeys(files, "h"), roles=roles, agents=agents),
        may_write=False,
        today=date.today(),
    )
    message = user_message(card=card, latest_change="none", editor="none", message="hi")

    lines = {line.split(":", 1)[0]: line for line in card.splitlines()}
    for label, listed in (("Files", files), ("Roles", roles), ("Agents", agents)):
        line = lines[label]
        body, _, more = line.removeprefix(f"{label}: ").partition(" (and ")
        shown = body.split(", ")
        assert set(shown) <= set(listed)  # no name cut
        assert more == f"{60 - len(shown)} more)"
    assert card in message  # the card is never cut as a whole


def test_when_a_change_created_a_file_then_its_before_side_says_new_file():
    change = {
        "change_id": "chg_1",
        "files": [{"name": "caps/new.yaml", "before_hash": None, "after_hash": "h1"}],
        "checks": [{"id": "yaml", "status": "pass"}],
    }

    assert "caps/new.yaml: new file" in latest_change_text(change)
