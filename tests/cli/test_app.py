"""Tests for inline terminal app rendering helpers."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console, Group

from fortify.agents import loader
from fortify.cli.app import (
    AgentRuntime,
    DOG_LOGO,
    _build_approval_handler,
    _load_agent_script,
    _prompt_for_approval,
    _render_current_run,
    _render_welcome,
    _tail_text,
)
from fortify.cli.state import LiveRunState, ToolActivity
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

    approved = _prompt_for_approval(
        console,
        {
            "tool_name": "write_file",
            "arguments": {
                "file_path": "napoleon.md",
                "content": "new section",
            },
        },
    )

    rendered = console.export_text()

    assert approved is True
    assert "Approval required for write_file" in rendered
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
