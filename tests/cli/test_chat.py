"""Tests for inline terminal app rendering helpers."""

from __future__ import annotations

from collections import deque
from pathlib import Path

from rich.console import Console, Group

from fortify.agents import loader
from fortify.cli._common import (
    AgentRuntime,
    build_approval_handler as _build_approval_handler,
    load_agent_script as _load_agent_script,
    prompt_for_approval as _prompt_for_approval,
)
from fortify.cli.chat import (
    DOG_LOGO,
    _drain_decisions,
    _render_current_run,
    _render_decision_panel,
    _render_welcome,
    _tail_text,
)
from fortify.cli.state import LiveRunState, ToolActivity
from fortify.security.decision import Decision, DecisionOutcome
from fortify.streaming import ToolCallState
from fortify.tools import edit_file, read_file

# Repo root, derived from this test file's location — keeps the
# ``_load_agent_script`` tests below portable across machines / CI.
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"


def test_tail_text_keeps_last_lines_of_long_output() -> None:
    """Keep only the trailing lines for live rendering."""
    text = "\n".join(f"line {index}" for index in range(1, 21))

    tailed, truncated = _tail_text(text, max_lines=4, max_chars=10_000)

    assert tailed == "line 17\nline 18\nline 19\nline 20"
    assert truncated is True


def test_tail_text_caps_large_character_payloads() -> None:
    """Trim very large text blocks before line tailing."""
    text = "a" * 50 + "tail"

    tailed, truncated = _tail_text(text, max_lines=5, max_chars=8)

    assert tailed == "aaaatail"
    assert truncated is True


def test_tail_text_reports_when_text_is_not_truncated() -> None:
    """Leave short text untouched and mark it as fully visible."""
    tailed, truncated = _tail_text("short answer", max_lines=5, max_chars=100)

    assert tailed == "short answer"
    assert truncated is False


def test_render_welcome_includes_agent_and_model() -> None:
    """Render a startup card with the active runtime metadata."""
    runtime = AgentRuntime(
        agent="agent",  # type: ignore[arg-type]
        handler="handler",  # type: ignore[arg-type]
        agent_name="example_agent",
        agent_source="local",
        model="gpt-5.4",
        tools_by_name={},
    )

    console = Console(record=True, width=100)
    console.print(_render_welcome(runtime))
    rendered = console.export_text()

    assert "example_agent" in rendered
    assert "gpt-5.4" in rendered
    assert "fortify" in rendered
    assert DOG_LOGO.splitlines()[0].strip() in rendered


def test_render_current_run_uses_tool_arguments_and_timeline_spacing() -> None:
    """Render tool rows with readable labels and connector spacing."""
    runtime = AgentRuntime(
        agent="agent",  # type: ignore[arg-type]
        handler="handler",  # type: ignore[arg-type]
        agent_name="repo_editor",
        agent_source="local",
        model="gpt-5.4",
        tools_by_name={
            read_file.name: read_file,
            edit_file.name: edit_file,
        },
    )
    current_run = LiveRunState(
        query="update napoleon.md",
        is_streaming=False,
        tools=[
            ToolActivity(
                tool_id="tool-1",
                tool_name="read_file",
                status=ToolCallState.COMPLETED,
                arguments={"file_path": "napoleon.md"},
            ),
            ToolActivity(
                tool_id="tool-2",
                tool_name="edit_file",
                status=ToolCallState.COMPLETED,
                arguments={"file_path": "napoleon.md"},
            ),
        ],
    )

    console = Console(record=True, width=100)
    console.print(Group(*_render_current_run(runtime, current_run, live=True)))
    rendered = console.export_text()

    assert "reading napoleon.md" in rendered
    assert "editing napoleon.md" in rendered
    assert "│" in rendered


def test_render_current_run_uses_spinner_for_thinking_state() -> None:
    """Render a live spinner when the assistant is still thinking."""
    runtime = AgentRuntime(
        agent="agent",  # type: ignore[arg-type]
        handler="handler",  # type: ignore[arg-type]
        agent_name="repo_editor",
        agent_source="local",
        model="gpt-5.4",
        tools_by_name={},
    )
    current_run = LiveRunState(
        query="think a bit",
        is_streaming=True,
    )

    console = Console(record=True, width=100)
    console.print(Group(*_render_current_run(runtime, current_run, live=True)))
    rendered = console.export_text()

    assert "thinking..." in rendered


def test_build_approval_handler_supports_auto_modes() -> None:
    """Map CLI approval modes to simple boolean handlers when possible."""
    console = Console(record=True, width=100)

    assert _build_approval_handler(console, "auto-approve") is True
    assert _build_approval_handler(console, "auto-deny") is False


def test_prompt_for_approval_asks_user_with_tool_arguments() -> None:
    """Render a compact approval prompt and return the user's decision."""
    console = Console(record=True, width=100)
    captured_prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        captured_prompts.append(prompt)
        return "y"

    console.input = fake_input  # type: ignore[method-assign]

    decision = Decision(
        outcome=DecisionOutcome.NEEDS_APPROVAL,
        agent_name="test-agent",
        tool_name="write_file",
        role="support",
        reason='Policy requires approval for tool "write_file"',
        error_type="approval_required",
        arguments={
            "file_path": "napoleon.md",
            "content": "new section",
        },
    )

    approved = _prompt_for_approval(console, decision)

    rendered = console.export_text()

    assert approved is True
    assert "Approval required for write_file" in rendered
    assert "role: support" in rendered
    assert "file_path: napoleon.md" in rendered
    assert "content: new section" in rendered
    assert "Type y to approve or n to deny, then press Enter." in rendered
    assert captured_prompts == ["[bold yellow]Approve? [y/N] [/]"]


def test_load_agent_script_registers_code_agents() -> None:
    """Importing a registration script should populate the code agent registry."""
    loader.clear_registered_agents()

    _load_agent_script(str(EXAMPLES_DIR / "file_agents.py"))

    assert "workspace_explorer" in loader.list_registered_agents()
    assert "repo_editor" in loader.list_registered_agents()


def test_load_research_agent_script_registers_update_researcher() -> None:
    """Importing the research example script should register the update researcher agent."""
    loader.clear_registered_agents()

    _load_agent_script(str(EXAMPLES_DIR / "research_agents.py"))

    assert "update_researcher" in loader.list_registered_agents()


def test_load_bash_file_agent_script_registers_repo_operator() -> None:
    """Importing the bash+file example script should register the repo operator agent."""
    loader.clear_registered_agents()

    _load_agent_script(str(EXAMPLES_DIR / "bash_file_agents.py"))

    assert "repo_operator" in loader.list_registered_agents()


# ---------------------------------------------------------------------------
# Decision panel — surfaces denies / approvals in the REPL
# ---------------------------------------------------------------------------


def _decision(
    outcome: DecisionOutcome,
    *,
    reason: str = "",
    error_type: str | None = None,
    role: str | None = None,
    violations: tuple[str, ...] = (),
    hint: object | None = None,
) -> Decision:
    """Build a Decision directly — bypasses the engine + verdict path
    for tests that only care about the rendered output."""
    return Decision(
        outcome=outcome,
        agent_name="r",
        tool_name="read_file",
        role=role,
        reason=reason,
        error_type=error_type,
        violations=violations,
        hint=hint,
    )


def test_render_decision_panel_returns_none_for_allow() -> None:
    """ALLOW is muted: a chatty REPL with one panel per tool call is
    noise. The whole point of the feed is 'what got blocked.'"""
    assert _render_decision_panel(_decision(DecisionOutcome.ALLOW)) is None


def test_render_decision_panel_for_deny_shows_reason_and_violations() -> None:
    """A DENY panel must surface the reason, error_type, role, and any
    violation strings — that's the diagnostic payload a dev needs."""
    decision = _decision(
        DecisionOutcome.DENY,
        reason="path escapes workspace",
        error_type="policy_denied",
        role="analyst",
        violations=("path_outside_glob", "read_denied_for_role"),
    )

    console = Console(record=True, width=100)
    console.print(_render_decision_panel(decision))
    rendered = console.export_text()

    assert "deny" in rendered
    assert "read_file" in rendered
    assert "path escapes workspace" in rendered
    assert "policy_denied" in rendered
    assert "analyst" in rendered
    assert "path_outside_glob" in rendered
    assert "read_denied_for_role" in rendered


def test_render_decision_panel_for_needs_approval() -> None:
    """NEEDS_APPROVAL renders distinctly (different glyph, different
    border) but carries the same diagnostic payload as DENY."""
    decision = _decision(
        DecisionOutcome.NEEDS_APPROVAL,
        reason="bash_run requires sign-off",
        error_type="approval_required",
    )

    console = Console(record=True, width=100)
    console.print(_render_decision_panel(decision))
    rendered = console.export_text()

    assert "needs_approval" in rendered
    assert "bash_run requires sign-off" in rendered
    assert "approval_required" in rendered


def test_render_decision_panel_handles_minimal_decision() -> None:
    """A DENY with no reason / violations / role still renders without
    crashing — defensive against engines that omit detail fields."""
    console = Console(record=True, width=100)
    console.print(_render_decision_panel(_decision(DecisionOutcome.DENY)))
    rendered = console.export_text()
    assert "deny" in rendered
    assert "read_file" in rendered


def test_drain_decisions_prints_only_deny_and_approval() -> None:
    """The deque drain helper consumes the whole queue but only prints
    panels for DENY / NEEDS_APPROVAL. ALLOWs are popped silently —
    leaving them in the deque across turns would mean the next turn
    suddenly sees old allows."""
    pending: deque[Decision] = deque(
        [
            _decision(DecisionOutcome.ALLOW, reason="silent"),
            _decision(DecisionOutcome.DENY, reason="visible deny"),
            _decision(DecisionOutcome.ALLOW, reason="silent again"),
            _decision(DecisionOutcome.NEEDS_APPROVAL, reason="visible approval"),
        ]
    )

    console = Console(record=True, width=100)
    _drain_decisions(console, pending)
    rendered = console.export_text()

    assert "visible deny" in rendered
    assert "visible approval" in rendered
    assert "silent" not in rendered
    # The deque must be empty after drain regardless of which entries
    # produced panels — otherwise the next turn would re-render them.
    assert len(pending) == 0


def test_drain_decisions_noop_on_empty_deque() -> None:
    """No decisions → no output. Empty turns (the agent answered without
    any tool call) must not print a stray panel separator."""
    pending: deque[Decision] = deque()
    console = Console(record=True, width=100)
    _drain_decisions(console, pending)
    assert console.export_text() == ""
