"""Version-compat probe for the google-adk adapter.

Wrap seam: ``BaseTool.run_async(*, args, tool_context)`` (patched) and
``agent.model_copy(update={"tools": ...})`` — the ``Agent`` must stay a
pydantic model. See ``hexgate/adapters/google/``.

``test_skills_contract`` probes a second surface: the ``@experimental`` ADK
skills API that skill discovery and gating read. It asserts on ADK only and
imports nothing from ``hexgate``.
"""

from __future__ import annotations

import inspect
import os
from typing import TYPE_CHECKING

import pytest

from tests.framework_compat import _probe
from tests.framework_compat._probe import ALLOWED_TOOL, DENIED_TOOL, DENY_MARKER
from tests.framework_compat.conftest import AGENT_NAMES

if TYPE_CHECKING:
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.skill_toolset import SkillToolset

pytestmark = pytest.mark.framework_compat

# All probes run on OpenAI + OPENAI_API_KEY so the matrix needs a single
# provider key. Google ADK is model-native to Gemini, so it reaches OpenAI
# through its LiteLLM wrapper. Construction is keyless (Tier 0/1 stays
# offline); only the Tier 2 run touches OPENAI_API_KEY.
MODEL = "openai/gpt-4o-mini"


def get_weather(city: str) -> str:
    _probe.record_execution(ALLOWED_TOOL)
    return f"{city}: sunny, 21C"


def delete_user(user_id: str) -> str:
    _probe.record_execution(DENIED_TOOL)
    return f"deleted {user_id}"


def _build_agent():
    from google.adk.agents import Agent
    from google.adk.models.lite_llm import LiteLlm

    return Agent(
        name=AGENT_NAMES["google"],
        model=LiteLlm(model=MODEL),
        tools=[get_weather, delete_user],
    )


def _build_wrapped():
    from hexgate.adapters.google.wrapper import wrap_google_agent

    return wrap_google_agent(_build_agent(), api_key="local-probe-key")


def test_contract():
    """Tier 0 — the ADK types + run_async surface the adapter needs exist."""
    from google.adk.agents import BaseAgent  # noqa: F401
    from google.adk.apps import App  # noqa: F401
    from google.adk.runners import Runner  # noqa: F401
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.function_tool import FunctionTool  # noqa: F401
    from google.adk.tools.tool_context import ToolContext  # noqa: F401

    assert hasattr(BaseTool, "run_async")


async def test_deny_path_blocks_and_does_not_execute(probe_context):
    """Tier 1 — the denied tool's run_async returns the deny marker."""
    wrapped, _binding = _build_wrapped()
    tool = next(t for t in wrapped.tools if t.name == DENIED_TOOL)
    with probe_context.sync_scope():
        # tool_context is unused on the deny short-circuit
        result = await tool.run_async(args={"user_id": "u1"}, tool_context=None)
    assert DENY_MARKER in str(result)
    assert not _probe.was_executed(DENIED_TOOL)


def test_allow_decision(probe_context):
    """Tier 1 — the resolved policy allows the allowed tool."""
    _wrapped, binding = _build_wrapped()
    with probe_context.sync_scope():
        decision = binding.enforcer.decide(ALLOWED_TOOL, {"city": "Paris"})
    assert decision.allowed


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="Tier 2 e2e needs OPENAI_API_KEY (LiteLLM → OpenAI)",
)
async def test_e2e_allow_executes(probe_context):
    """Tier 2 — a full runner drives the seam and runs the allowed tool."""
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from hexgate.adapters.google import HexgateRunner

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="version_probe",
        user_id=probe_context.user_id,
        session_id=probe_context.session_id,
    )
    runner = HexgateRunner(
        agent=_build_agent(),
        app_name="version_probe",
        session_service=session_service,
        api_key="local-probe-key",
    )
    message = types.Content(role="user", parts=[types.Part(text="Weather in Tokyo?")])
    async for _event in runner.run_async(
        new_message=message, hexgate_context=probe_context
    ):
        pass
    assert _probe.was_executed(ALLOWED_TOOL)


# --- ADK skills contract (PR A4) -------------------------------------------
#
# The skills feature is @experimental upstream and the discovery/gating work
# reaches into internals that carry no stability promise. These names are the
# ones that work depends on; each assertion below says what it guards.

PROBE_SKILL_NAME = "contract-probe"
PROBE_SKILL_DESCRIPTION = "In-memory skill used to probe the ADK skills contract."
PROBE_SKILL_INSTRUCTIONS = "No-op probe instructions."
EXPERIMENTAL_WARNING_MATCH = "SKILL_TOOLSET"

SKILL_NAME_ARG = "skill_name"
FILE_PATH_ARG = "file_path"

SKILL_TOOL_CLASS_NAMES = (
    "ListSkillsTool",
    "LoadSkillTool",
    "LoadSkillResourceTool",
    "RunSkillScriptTool",
)
DELEGATED_TOOLSET_METHODS = (
    "get_tools_with_prefix",
    "get_auth_config",
    "close",
    "process_llm_request",
)
FRONTMATTER_FIELDS = ("name", "description", "allowed_tools", "metadata")
SKILL_FIELDS = ("frontmatter", "instructions", "resources")
RESOURCE_ENUMERATORS = ("list_references", "list_assets", "list_scripts")


def _build_probe_toolset() -> SkillToolset:
    """A toolset over one in-memory Skill — no fixture directory to fail on."""
    from google.adk.skills import Frontmatter, Skill
    from google.adk.tools.skill_toolset import SkillToolset

    skill = Skill(
        frontmatter=Frontmatter(
            name=PROBE_SKILL_NAME,
            description=PROBE_SKILL_DESCRIPTION,
        ),
        instructions=PROBE_SKILL_INSTRUCTIONS,
    )
    return SkillToolset(skills=[skill])


def _declared_arguments(tool: BaseTool) -> tuple[set[str], set[str]]:
    """(declared, required) argument names read off the FunctionDeclaration."""
    schema = tool._get_declaration().parameters_json_schema
    return set(schema["properties"]), set(schema.get("required", []))


def test_skills_contract():
    """Tier 0 — the ADK skills surfaces the skills work depends on still exist.

    Skipped below google-adk 1.25.0, where ``SkillToolset`` does not exist.
    A failure here means an ADK release moved something, not that hexgate is
    wrong: each assertion names the hexgate code it guards.
    """
    pytest.importorskip("google.adk.skills")
    skill_toolset = pytest.importorskip("google.adk.tools.skill_toolset")

    from google.adk.skills import (
        Frontmatter,
        Resources,
        Skill,
        list_skills_in_dir,
        load_skill_from_dir,
    )
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.base_toolset import BaseToolset

    # Guards the adapter's type routing: a toolset is not a tool, which is the
    # whole reason GuardedToolset exists alongside the per-tool wrap.
    assert not issubclass(BaseToolset, BaseTool)

    # Guards expand_toolset and GuardedToolset.get_tools — both await it.
    assert inspect.iscoroutinefunction(skill_toolset.SkillToolset.get_tools)

    # Guards what GuardedToolset delegates to the toolset it wraps.
    for method in DELEGATED_TOOLSET_METHODS:
        assert callable(getattr(BaseToolset, method)), method

    # Guards the tool-type → skill-level map used when gating skill calls.
    for class_name in SKILL_TOOL_CLASS_NAMES:
        assert issubclass(getattr(skill_toolset, class_name), BaseTool), class_name

    # Expected, not suppressed: the warning going away is itself news.
    with pytest.warns(UserWarning, match=EXPERIMENTAL_WARNING_MATCH):
        toolset = _build_probe_toolset()
        load_skill = skill_toolset.LoadSkillTool(toolset)
        run_skill_script = skill_toolset.RunSkillScriptTool(toolset)

    # Guards the invocation-cache opt-out the adapter sets on the wrapper.
    assert isinstance(toolset._use_invocation_cache, bool)

    # Guards the discovery route — private, the riskiest assertion here.
    assert PROBE_SKILL_NAME in toolset._skills

    # Guards reading the skill name out of a load_skill call's args.
    _declared, required = _declared_arguments(load_skill)
    assert required == {SKILL_NAME_ARG}

    # Guards the script-level policy key, built from both arguments.
    declared, required = _declared_arguments(run_skill_script)
    assert {SKILL_NAME_ARG, FILE_PATH_ARG} <= declared
    assert {SKILL_NAME_ARG, FILE_PATH_ARG} == required

    # Guards the manifest mapping of a discovered skill.
    for field in FRONTMATTER_FIELDS:
        assert field in Frontmatter.model_fields, field
    for field in SKILL_FIELDS:
        assert field in Skill.model_fields, field

    # Guards the L3 resource enumeration.
    for enumerator in RESOURCE_ENUMERATORS:
        assert callable(getattr(Resources, enumerator)), enumerator

    # Guards the public loaders documented as the alternative to _skills.
    assert callable(list_skills_in_dir)
    assert callable(load_skill_from_dir)
