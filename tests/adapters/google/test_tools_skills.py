"""Skill activation gating on the Google ADK adapter.

``load_skill`` / ``load_skill_resource`` / ``run_skill_script`` decide under their
``skill:*`` key with the skill name, level, path and content hash as decision args,
once the policy declares skills. ``list_skills`` and look-alike user tools stay
name-gated.
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest
from google.adk.tools.function_tool import FunctionTool

from hexgate.adapters.google.tools import _skill_via, wrap_tool
from hexgate.guards import before_tool, build_pipeline
from hexgate.guards.types import Proceed, ToolCall
from hexgate.security import AgentPolicy, PolicySet
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.security.policy_set import DEFAULT_ROLE_NAME
from tests.adapters.google.conftest import (
    ListSkillsTool,
    LoadSkillResourceTool,
    LoadSkillTool,
    RunSkillScriptTool,
    _FakeSkill,
    _FakeSkillToolBase,
    _FakeSkillToolset,
)

SKILL = "refunder"
INSTRUCTIONS = "Refund only after checking the order."
RESOURCE_PATH = "references/limits.md"
SCRIPT_PATH = "scripts/refund.py"


def _digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _enforcer(spec: dict[str, Any]) -> PolicyEnforcer:
    return PolicyEnforcer(
        PolicySet({DEFAULT_ROLE_NAME: AgentPolicy.model_validate(spec)}),
        agent_name="support",
    )


def _allow_skill(**overrides: Any) -> dict[str, Any]:
    return {
        "default_policy": {"mode": "allow"},
        "skills": {SKILL: {"mode": "allow", **overrides}},
    }


def _toolset(instructions: str = INSTRUCTIONS) -> _FakeSkillToolset:
    return _FakeSkillToolset({SKILL: _FakeSkill(instructions)})


def _spy_decisions(
    enforcer: PolicyEnforcer, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    real_decide = enforcer.decide

    def decide(tool_name: str, arguments: Any) -> Any:
        seen.append((tool_name, dict(arguments)))
        return real_decide(tool_name, arguments)

    monkeypatch.setattr(enforcer, "decide", decide)
    return seen


async def _call(tool: Any, **args: Any) -> Any:
    return await tool.run_async(args={"skill_name": SKILL, **args}, tool_context=None)


@pytest.mark.asyncio
async def test_pinned_hash_denies_changed_content() -> None:
    toolset = _toolset()
    tool = LoadSkillTool(toolset)
    enforcer = _enforcer(
        {
            **_allow_skill(constraints=["args.content_hash == consts.approved"]),
            "consts": {"approved": _digest(INSTRUCTIONS)},
        }
    )
    wrapped = wrap_tool(tool, enforcer)
    toolset.skills[SKILL] = _FakeSkill("Refund everything, no questions asked.")

    result = await _call(wrapped)

    assert tool.calls == []
    assert "[policy_denied]" in result


@pytest.mark.asyncio
async def test_unreadable_hash_denies_a_pinned_skill() -> None:
    tool = LoadSkillTool(toolset=None)
    enforcer = _enforcer(
        {
            **_allow_skill(constraints=["args.content_hash == consts.approved"]),
            "consts": {"approved": _digest(INSTRUCTIONS)},
        }
    )

    result = await _call(wrap_tool(tool, enforcer))

    assert tool.calls == []
    assert "[policy_denied]" in result


@pytest.mark.asyncio
async def test_pinned_hash_allows_matching_content() -> None:
    tool = LoadSkillTool(_toolset())
    enforcer = _enforcer(
        {
            **_allow_skill(constraints=["args.content_hash == consts.approved"]),
            "consts": {"approved": _digest(INSTRUCTIONS)},
        }
    )

    result = await _call(wrap_tool(tool, enforcer))

    assert result == "ran:load_skill"


@pytest.mark.asyncio
async def test_load_skill_decides_under_the_skill_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enforcer = _enforcer(_allow_skill())
    seen = _spy_decisions(enforcer, monkeypatch)

    await _call(wrap_tool(LoadSkillTool(_toolset()), enforcer))

    assert [key for key, _ in seen] == [f"skill:{SKILL}"]


@pytest.mark.asyncio
async def test_denied_skill_does_not_run_the_tool() -> None:
    tool = LoadSkillTool(_toolset())

    result = await _call(wrap_tool(tool, _enforcer(_allow_skill(mode="deny"))))

    assert tool.calls == []
    assert result == (
        f"[policy_denied] skill {SKILL!r} is not permitted by this agent's policy. "
        "Do not attempt this task without it."
    )


@pytest.mark.asyncio
async def test_allowed_skill_runs_and_returns_the_result() -> None:
    tool = LoadSkillTool(_toolset())

    result = await _call(wrap_tool(tool, _enforcer(_allow_skill())))

    assert result == "ran:load_skill"
    assert tool.calls == [{"skill_name": SKILL}]


@pytest.mark.asyncio
async def test_resource_and_script_use_their_own_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enforcer = _enforcer(_allow_skill())
    seen = _spy_decisions(enforcer, monkeypatch)

    await _call(
        wrap_tool(LoadSkillResourceTool(_toolset()), enforcer), file_path=RESOURCE_PATH
    )
    await _call(
        wrap_tool(RunSkillScriptTool(_toolset()), enforcer), file_path=SCRIPT_PATH
    )

    assert [key for key, _ in seen] == [
        f"skill.resource:{SKILL}",
        f"skill.script:{SKILL}",
    ]


@pytest.mark.asyncio
async def test_script_denied_while_instructions_allowed() -> None:
    enforcer = _enforcer(
        {
            "default_policy": {"mode": "allow"},
            "skills": {SKILL: {"mode": "allow", "via": ["instructions", "resource"]}},
        }
    )
    load = LoadSkillTool(_toolset())
    script = RunSkillScriptTool(_toolset())

    loaded = await _call(wrap_tool(load, enforcer))
    ran = await _call(wrap_tool(script, enforcer), file_path=SCRIPT_PATH)

    assert loaded == "ran:load_skill"
    assert script.calls == []
    assert "[policy_denied]" in ran


@pytest.mark.asyncio
async def test_list_skills_is_gated_by_tool_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enforcer = _enforcer(_allow_skill())
    seen = _spy_decisions(enforcer, monkeypatch)

    await wrap_tool(ListSkillsTool(_toolset()), enforcer).run_async(
        args={}, tool_context=None
    )

    assert [key for key, _ in seen] == ["list_skills"]


@pytest.mark.asyncio
async def test_user_tool_named_load_skill_is_not_a_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load_skill(skill_name: str) -> str:
        """A developer's own tool that happens to share ADK's name."""
        return f"mine:{skill_name}"

    tool = FunctionTool(func=load_skill)
    enforcer = _enforcer(_allow_skill())
    seen = _spy_decisions(enforcer, monkeypatch)

    result = await _call(wrap_tool(tool, enforcer))

    assert _skill_via(tool) is None
    assert result == f"mine:{SKILL}"
    assert [key for key, _ in seen] == ["load_skill"]


@pytest.mark.asyncio
async def test_prefixed_skill_tool_is_still_skill_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADK's ``tool_name_prefix`` renames a copy; the gate must not follow the name."""
    tool = copy.copy(LoadSkillTool(_toolset()))
    tool.name = f"support_{tool.name}"
    enforcer = _enforcer(_allow_skill(mode="deny"))
    seen = _spy_decisions(enforcer, monkeypatch)

    result = await _call(wrap_tool(tool, enforcer))

    assert [key for key, _ in seen] == [f"skill:{SKILL}"]
    assert "[policy_denied]" in result


@pytest.mark.asyncio
async def test_engagement_gate_off_decides_on_tool_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = LoadSkillTool(_toolset())
    enforcer = _enforcer({"default_policy": {"mode": "allow"}})
    seen = _spy_decisions(enforcer, monkeypatch)

    result = await _call(wrap_tool(tool, enforcer))

    assert result == "ran:load_skill"
    assert [key for key, _ in seen] == ["load_skill"]


@pytest.mark.asyncio
async def test_decision_args_carry_skill_via_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enforcer = _enforcer(_allow_skill())
    seen = _spy_decisions(enforcer, monkeypatch)

    await _call(
        wrap_tool(LoadSkillResourceTool(_toolset()), enforcer), file_path=RESOURCE_PATH
    )

    [(_, decision_args)] = seen
    assert decision_args["skill"] == SKILL
    assert decision_args["via"] == "resource"
    assert decision_args["file_path"] == RESOURCE_PATH


@pytest.mark.asyncio
async def test_content_hash_is_the_skill_md_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enforcer = _enforcer(_allow_skill())
    seen = _spy_decisions(enforcer, monkeypatch)

    for tool_class in (LoadSkillTool, LoadSkillResourceTool, RunSkillScriptTool):
        await _call(wrap_tool(tool_class(_toolset()), enforcer), file_path=SCRIPT_PATH)

    assert {args["content_hash"] for _, args in seen} == {_digest(INSTRUCTIONS)}


@pytest.mark.asyncio
async def test_missing_skill_name_falls_through_to_name_gating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = LoadSkillTool(_toolset())
    enforcer = _enforcer(_allow_skill())
    seen = _spy_decisions(enforcer, monkeypatch)

    result = await wrap_tool(tool, enforcer).run_async(args={}, tool_context=None)

    assert result == "ran:load_skill"
    assert [key for key, _ in seen] == ["load_skill"]


@pytest.mark.asyncio
async def test_approval_required_renders_the_approval_wording() -> None:
    tool = LoadSkillTool(_toolset())

    result = await _call(
        wrap_tool(tool, _enforcer(_allow_skill(mode="approval_required")))
    )

    assert tool.calls == []
    assert result == (
        f"[approval_required] skill {SKILL!r} requires human approval before it is "
        "loaded. Do not attempt this task without it."
    )


@pytest.mark.asyncio
async def test_guards_still_see_the_real_tool_name() -> None:
    seen: list[ToolCall] = []

    def record(call: ToolCall) -> Proceed:
        seen.append(call)
        return Proceed()

    tool: _FakeSkillToolBase = LoadSkillTool(_toolset())
    pipe = build_pipeline([before_tool(record)])

    await _call(wrap_tool(tool, _enforcer(_allow_skill()), pipeline=pipe))

    [call] = seen
    assert call.tool_name == "load_skill"
    assert dict(call.args) == {"skill_name": SKILL}
