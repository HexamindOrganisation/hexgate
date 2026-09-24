"""Version-compat probe for the google-adk adapter.

Wrap seam: ``BaseTool.run_async(*, args, tool_context)`` (patched) and
``agent.model_copy(update={"tools": ...})`` — the ``Agent`` must stay a
pydantic model. See ``hexgate/adapters/google/``.

``test_skills_surface`` probes a second surface: the ``@experimental`` ADK skills
API that skill discovery and gating read. It asserts on ADK only and imports
nothing from ``hexgate``. It carries its own tier (T3) so that churn in an
experimental upstream feature cannot condemn the whole ADK cell.
"""

from __future__ import annotations

import inspect
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_dist_version
from typing import TYPE_CHECKING

import pytest
from packaging.version import Version

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

ADK_DIST = "google-adk"
SKILLS_FLOOR = Version("1.25.0")

PROBE_SKILL_NAME = "surface-probe"
PROBE_SKILL_DESCRIPTION = "In-memory skill used to probe the ADK skills surface."
PROBE_SKILL_INSTRUCTIONS = "No-op probe instructions."
EXPERIMENTAL_WARNING_MATCH = "SKILL_TOOLSET"

SKILL_NAME_ARG = "skill_name"
FILE_PATH_ARG = "file_path"
READONLY_CONTEXT_ARG = "readonly_context"
PROCESS_LLM_REQUEST_PARAMS = frozenset({"self", "tool_context", "llm_request"})

# Two calls through a cache-disabled toolset must reach ``get_tools`` twice; with
# the cache on, the second is served from the invocation cache and never arrives.
EXPECTED_UNCACHED_CALLS = 2

SKILL_TOOL_CLASS_NAMES = (
    "ListSkillsTool",
    "LoadSkillTool",
    "LoadSkillResourceTool",
    "RunSkillScriptTool",
)
# Only the two hexgate forwards verbatim. ``get_tools_with_prefix`` and
# ``process_llm_request`` get signature assertions below instead, because
# existence is not what breaks the delegation.
DELEGATED_TOOLSET_METHODS = ("get_auth_config", "close")
FRONTMATTER_FIELDS = ("name", "description", "allowed_tools", "metadata")
SKILL_FIELDS = ("frontmatter", "instructions", "resources")
RESOURCE_ENUMERATORS = ("list_references", "list_assets", "list_scripts")


def _adk_below_skills_floor() -> bool:
    """True when the installed ADK predates ``SkillToolset``.

    Gated on the version rather than on ``importorskip`` deliberately. Above the
    floor the skills modules *must* import, and an ImportError there is precisely
    the drift this probe exists to catch — an ``@experimental`` feature graduating
    to stable is a plausible way for those module paths to move. An
    ``importorskip`` would turn that into a skip, rendering ``–`` in the matrix's
    T3 column: the same glyph as the legitimate pre-floor skip, under a Status
    that stays OK because T3 never reaches ``classify``. The probe would go quiet
    exactly when it should fire, and nothing else would notice — ``_skill_objects``
    in ``hexgate/manifest/google.py`` reads ``_skills`` through
    ``getattr(..., None)`` and degrades to "no skills in the manifest" rather than
    raising, so a renamed attribute silently empties every manifest.

    A missing distribution skips: ``test_contract`` already fails loudly on that,
    and a second confusing error adds nothing.
    """
    try:
        return Version(installed_dist_version(ADK_DIST)) < SKILLS_FLOOR
    except PackageNotFoundError:
        return True


def _arm_experimental_warning() -> None:
    """Clear ADK's once-per-process latch for the skills feature's warning.

    ``@experimental`` routes through ``_emit_non_stable_warning_once``, which
    dedupes in a process-global set, so whichever test constructs a skills object
    first consumes the only warning the process will ever emit. Without this reset
    the ``pytest.warns`` below asserts test ordering rather than ADK's behaviour.

    Reaching into the private registry is deliberate: if the latch moves, this
    probe should fail loudly rather than quietly stop checking anything.
    """
    from google.adk.features import FeatureName, _feature_registry

    _feature_registry._WARNED_FEATURES.discard(FeatureName.SKILL_TOOLSET)


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


async def _uncached_get_tools_calls(base_toolset_cls: type) -> int:
    """How many times ``get_tools_with_prefix`` reaches ``get_tools`` over two calls.

    The behavioural half of the ``_use_invocation_cache`` check: asserting the
    attribute exists only catches its removal, not the case that actually bites —
    the flag surviving while ``get_tools_with_prefix`` stops consulting it, which
    silently re-enables caching on ``GuardedToolset`` and lets late tools arrive
    ungated (``hexgate/adapters/google/tools.py``).
    """
    calls = 0

    class _CountingToolset(base_toolset_cls):
        async def get_tools(self, readonly_context=None):
            nonlocal calls
            calls += 1
            return []

    toolset = _CountingToolset()
    toolset._use_invocation_cache = False
    await toolset.get_tools_with_prefix(None)
    await toolset.get_tools_with_prefix(None)
    return calls


@pytest.mark.skipif(
    _adk_below_skills_floor(),
    reason=f"SkillToolset exists from {ADK_DIST} {SKILLS_FLOOR}",
)
async def test_skills_surface():
    """Tier 3 — the ADK skills surfaces the skills work depends on still exist.

    Its own tier, not Tier 0: this probes an ``@experimental`` upstream feature,
    and churn there must not classify the whole ADK cell UNUSABLE while the
    wrap/deny seam Tier 0 and Tier 1 cover is perfectly fine.

    Skipped below google-adk 1.25.0, where ``SkillToolset`` does not exist. Above
    it the imports below are unguarded on purpose: see ``_adk_below_skills_floor``.
    A failure here means an ADK release moved something, not that hexgate is
    wrong: each assertion names the hexgate code it guards.
    """
    from google.adk.skills import (
        Frontmatter,
        Resources,
        Skill,
        list_skills_in_dir,
        load_skill_from_dir,
    )
    from google.adk.tools import skill_toolset
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.base_toolset import BaseToolset

    # Guards the adapter's type routing: a toolset is not a tool, which is the
    # whole reason GuardedToolset exists alongside the per-tool wrap.
    assert not issubclass(BaseToolset, BaseTool)

    # Guards expand_toolset and GuardedToolset.get_tools — both await it.
    assert inspect.iscoroutinefunction(skill_toolset.SkillToolset.get_tools)

    # Guards what GuardedToolset forwards verbatim.
    for method in DELEGATED_TOOLSET_METHODS:
        assert callable(getattr(BaseToolset, method)), method

    # hexgate passes readonly_context positionally (adapters/google/tools.py and
    # manifest/google.py), so a renamed parameter is harmless here and only a move
    # to keyword-only would break the call. Assert the shape that actually bites.
    prefix_params = inspect.signature(BaseToolset.get_tools_with_prefix).parameters
    assert READONLY_CONTEXT_ARG in prefix_params
    assert (
        prefix_params[READONLY_CONTEXT_ARG].kind is not inspect.Parameter.KEYWORD_ONLY
    )

    # This one hexgate calls by keyword and ADK already declares keyword-only, so
    # a rename or a move to positional-only is what breaks the delegation.
    assert (
        set(inspect.signature(BaseToolset.process_llm_request).parameters)
        == PROCESS_LLM_REQUEST_PARAMS
    )

    # Guards the tool-type → skill-level map used when gating skill calls.
    for class_name in SKILL_TOOL_CLASS_NAMES:
        assert issubclass(getattr(skill_toolset, class_name), BaseTool), class_name

    # Expected, not suppressed: the warning going away is itself news. Armed first
    # because ADK emits it once per process, so without the reset this asserts
    # whichever test constructed a skills object first, not ADK's behaviour.
    _arm_experimental_warning()
    with pytest.warns(UserWarning, match=EXPERIMENTAL_WARNING_MATCH):
        toolset = _build_probe_toolset()

    # Outside the block on purpose: both tool classes are @experimental too, and
    # are silent here only because the construction above consumed the shared
    # dedupe key. Inside, they would look checked without being checked.
    load_skill = skill_toolset.LoadSkillTool(toolset)
    run_skill_script = skill_toolset.RunSkillScriptTool(toolset)

    # Guards the invocation-cache opt-out the adapter sets on the wrapper — that
    # the flag exists *and* that get_tools_with_prefix still consults it.
    assert toolset._use_invocation_cache is False
    assert await _uncached_get_tools_calls(BaseToolset) == EXPECTED_UNCACHED_CALLS

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
