"""Google ADK adapter: wrap ``BaseTool`` so ``run_async`` consults a
:class:`PolicyEnforcer` first. Non-allow outcomes render as markered
strings the model sees as tool output.

When a caller supplies ``approval_handler``, a ``NEEDS_APPROVAL``
decision fires the callback and runs the original tool on truthy return;
falsy return (or a missing handler) keeps today's behavior of surfacing
the ``[approval_required]`` marker to the model.
"""

from __future__ import annotations

import copy
import functools
import hashlib
from collections.abc import Callable
from typing import Any, Union

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.auth.auth_tool import AuthConfig
from google.adk.models.llm_request import LlmRequest
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext

from hexgate.approvals import ApprovalHandler
from hexgate.guards.runner import RenderError, run_guarded_async
from hexgate.guards.types import ToolPipeline
from hexgate.security.decision import DecisionOutcome
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.security.models import SkillVia, agent_target_key, skill_key
from hexgate.security.naming import canonical_name

ToolEntry = Union[BaseTool, BaseToolset, Callable[..., Any]]

_SKILL_TOOLSET_MODULE = "google.adk.tools.skill_toolset"
_SKILL_NAME_ARG = "skill_name"
_FILE_PATH_ARG = "file_path"
_TOOLSET_ATTR = "_toolset"
_GET_SKILL_ATTR = "_get_skill"
_CONTENT_HASH_PREFIX = "sha256:"

# Keyed on the class, not the tool name: ADK's tool_name_prefix renames the copy
# (``<prefix>_load_skill``), and a name lookup would drop a prefixed skill tool back
# to name gating. ``ListSkillsTool`` is absent on purpose — the catalog is already
# in the system prompt, so gating it only hides what exists.
_SKILL_VIA_BY_TOOL_CLASS: dict[str, SkillVia] = {
    "LoadSkillTool": "instructions",
    "LoadSkillResourceTool": "resource",
    "RunSkillScriptTool": "script",
}


def _render_error(decision: Any) -> str:
    """Google renders a blocked decision as a string tool result."""
    return decision.as_error_message()


def _render_reach_error(target: str) -> RenderError:
    """Model-facing renderer for a denied/held agent-as-tool reach — keeps the
    ``[marker] …`` shape but names the bare target, not the ``agent.tool:`` key.
    Mirrors the OpenAI adapter."""

    def render(decision: Any) -> str:
        marker = decision.error_type or decision.outcome.value
        if decision.outcome is DecisionOutcome.NEEDS_APPROVAL:
            body = f"reach to agent {target!r} requires human approval before it runs"
        else:
            body = f"reach to agent {target!r} is not permitted by this agent's policy"
        return f"[{marker}] {body}. The sub-agent was not invoked."

    return render


def _agent_tool_target(base: BaseTool) -> str | None:
    """Canonical target-agent name if ``base`` is an ``AgentTool``, else ``None``.

    An ``AgentTool`` names itself after its wrapped agent (``AgentTool.name ==
    agent.name``), so gating it by name would double-decide the delegation the
    transfer plugin already governs; instead we gate it under its reach key."""
    if isinstance(base, AgentTool):
        target = getattr(base.agent, "name", None)
        if target:
            return canonical_name(target)
    return None


def _render_skill_error(skill: str) -> RenderError:
    """Model-facing renderer for a denied/held skill activation.

    The closing sentence is deliberate: without it a model tends to improvise the
    procedure from memory, dropping exactly the guardrails the skill encoded."""

    def render(decision: Any) -> str:
        marker = decision.error_type or decision.outcome.value
        if decision.outcome is DecisionOutcome.NEEDS_APPROVAL:
            body = f"skill {skill!r} requires human approval before it is loaded"
        else:
            body = f"skill {skill!r} is not permitted by this agent's policy"
        return f"[{marker}] {body}. Do not attempt this task without it."

    return render


def _skill_via(base: BaseTool) -> SkillVia | None:
    """The disclosure level ``base`` gates, or None if it is not a skill tool.

    Keyed on the defining module rather than the imported classes: they exist only
    from ADK 1.25.0 and pyproject pins >=1.14. A user's own tool named
    ``load_skill`` is defined elsewhere, so it does not match — nor does a subclass
    of ``LoadSkillTool`` defined outside ADK.
    """
    tool_class = type(base)
    if tool_class.__module__ != _SKILL_TOOLSET_MODULE:
        return None
    return _SKILL_VIA_BY_TOOL_CLASS.get(tool_class.__name__)


def _skill_content_hash(base: BaseTool, skill_name: str) -> str | None:
    """sha256 of the skill's SKILL.md body, or None if it cannot be read.

    Reaches through ADK privates (``_toolset`` on the tool, ``_get_skill`` on the
    toolset), so every hop is guarded and an ADK refactor degrades to no hash. None
    is safe: a policy pinning a hash fails closed on an absent argument.
    """
    toolset = getattr(base, _TOOLSET_ATTR, None)
    get_skill = getattr(toolset, _GET_SKILL_ATTR, None)
    if not callable(get_skill):
        return None
    instructions = getattr(get_skill(skill_name), "instructions", None)
    if not isinstance(instructions, str):
        return None
    digest = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
    return f"{_CONTENT_HASH_PREFIX}{digest}"


def _skill_decision(
    base: BaseTool, args: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """Policy key + decision args when this call reaches a skill, else None."""
    via = _skill_via(base)
    if via is None:
        return None
    skill_name = args.get(_SKILL_NAME_ARG)
    if not isinstance(skill_name, str) or not skill_name.strip():
        # Left to name gating; ADK itself answers this with INVALID_ARGUMENTS.
        return None
    return skill_key(via, skill_name), {
        "skill": skill_name,
        "via": via,
        _FILE_PATH_ARG: args.get(_FILE_PATH_ARG),
        "content_hash": _skill_content_hash(base, skill_name),
    }


def _normalize(tool: ToolEntry) -> BaseTool:
    """Coerce a tool entry into a ``BaseTool`` (plain callables → FunctionTool)."""
    if isinstance(tool, BaseTool):
        return tool
    if callable(tool):
        return FunctionTool(func=tool)
    raise TypeError(
        f"Cannot install policy on tool {tool!r}: expected google.adk BaseTool "
        f"or callable, got {type(tool).__name__}."
    )


def wrap_tool(
    tool: ToolEntry,
    enforcer: PolicyEnforcer,
    *,
    approval_handler: ApprovalHandler | None = None,
    pipeline: ToolPipeline | None = None,
) -> BaseTool:
    """Return a copy of ``tool`` with ``run_async`` gated by ``enforcer``.

    Routes through the shared :func:`run_guarded_async`, so before/after
    guards run around the policy check exactly as on the other adapters.
    """
    base = _normalize(tool)
    name = base.name
    original_run_async = base.run_async
    # An AgentTool is a reach edge; gate it under agent.tool:<target>, matching the
    # OpenAI adapter. ``None`` for an ordinary tool.
    reach_target = _agent_tool_target(base)

    @functools.wraps(original_run_async, updated=())
    async def guarded_run_async(
        *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        call_args = args or {}
        # Engagement read per call (hot-reload-safe). The skill name arrives in the
        # call's args, so unlike the reach target it cannot be resolved at wrap time.
        skill = (
            _skill_decision(base, call_args)
            if enforcer.policy.declares_skills()
            else None
        )
        policy_key: str | None
        policy_args: dict[str, Any] | None
        render_policy_error: RenderError | None
        if skill is not None:
            policy_key, policy_args = skill
            render_policy_error = _render_skill_error(policy_args["skill"])
        # Gate the reach key only when the policy declares *tool* reach — matching
        # OpenAI, so a handoff-only policy leaves as-tools name-gated on both
        # adapters rather than diverging.
        elif reach_target is not None and enforcer.policy.declares_tool_reach():
            policy_key = agent_target_key("tool", reach_target)
            policy_args = {
                "agent": enforcer.agent_name,
                "target": reach_target,
                "via": "tool",
            }
            # Reach wording on the policy denial only; a guard Halt still renders
            # through _render_error (see run_guarded_async).
            render_policy_error = _render_reach_error(reach_target)
        else:
            policy_key = None
            policy_args = None
            render_policy_error = None
        return await run_guarded_async(
            name,
            call_args,
            enforcer=enforcer,
            pipeline=pipeline,
            approval_handler=approval_handler,
            invoke=lambda final: original_run_async(
                args=final, tool_context=tool_context
            ),
            render_error=_render_error,
            policy_key=policy_key,
            policy_args=policy_args,
            render_policy_error=render_policy_error,
        )

    wrapped = copy.copy(base)
    wrapped.run_async = guarded_run_async
    return wrapped


class GuardedToolset(BaseToolset):
    """A :class:`BaseToolset` whose every yielded tool is policy-gated.

    Resolves the inner toolset on each call rather than snapshotting once, so
    tools a toolset only exposes later in a run are gated too.
    """

    def __init__(
        self,
        inner: BaseToolset,
        enforcer: PolicyEnforcer,
        *,
        approval_handler: ApprovalHandler | None = None,
        pipeline: ToolPipeline | None = None,
    ) -> None:
        # Deliberately no tool_filter/tool_name_prefix: the inner has applied
        # both by the time we see its tools, and setting them here would
        # double-apply the prefix.
        super().__init__()
        self._inner = inner
        self._enforcer = enforcer
        self._approval_handler = approval_handler
        self._pipeline = pipeline
        # The inner toolset owns caching: ADK's final get_tools_with_prefix
        # memoizes per invocation, and a toolset whose list grows mid-run clears
        # the flag on itself. Caching here too would short-circuit before we ever
        # delegate, and the late tools would arrive ungated.
        self._use_invocation_cache = False

    async def get_tools(
        self, readonly_context: ReadonlyContext | None = None
    ) -> list[BaseTool]:
        # get_tools_with_prefix, not get_tools: it is what applies the inner's
        # tool_name_prefix, and the gate must key on the name the model calls.
        return [
            wrap_tool(
                tool,
                self._enforcer,
                approval_handler=self._approval_handler,
                pipeline=self._pipeline,
            )
            for tool in await self._inner.get_tools_with_prefix(readonly_context)
        ]

    async def process_llm_request(
        self, *, tool_context: ToolContext, llm_request: LlmRequest
    ) -> None:
        registered_before = dict(llm_request.tools_dict)
        await self._inner.process_llm_request(
            tool_context=tool_context, llm_request=llm_request
        )
        self._gate_directly_registered(llm_request, registered_before)

    def _gate_directly_registered(
        self, llm_request: LlmRequest, registered_before: dict[str, BaseTool]
    ) -> None:
        """Gate tools the inner wrote straight into ``llm_request.tools_dict``.

        A toolset may bypass ``get_tools`` and register its tools itself — ADK's
        ComputerUseToolset does. Dispatch resolves calls out of ``tools_dict``,
        so anything the inner leaves there raw would run ungated: the gated
        copies from :meth:`get_tools` land under their own (possibly prefixed)
        names and never displace it.
        """
        for name, tool in list(llm_request.tools_dict.items()):
            if registered_before.get(name) is tool:
                continue
            llm_request.tools_dict[name] = wrap_tool(
                tool,
                self._enforcer,
                approval_handler=self._approval_handler,
                pipeline=self._pipeline,
            )

    def get_auth_config(self) -> AuthConfig | None:
        """ADK populates credentials from this before listing or running tools."""
        return self._inner.get_auth_config()

    async def close(self) -> None:
        await self._inner.close()


def wrap_tools(
    tools: list[ToolEntry],
    enforcer: PolicyEnforcer,
    *,
    approval_handler: ApprovalHandler | None = None,
    pipeline: ToolPipeline | None = None,
) -> list[BaseTool | BaseToolset]:
    """Return a fresh list of policy-gated copies.

    A ``BaseToolset`` entry is wrapped rather than expanded, so its tools are
    resolved — and gated — at each use rather than snapshotted here.
    """
    return [
        GuardedToolset(
            entry, enforcer, approval_handler=approval_handler, pipeline=pipeline
        )
        if isinstance(entry, BaseToolset)
        else wrap_tool(
            entry, enforcer, approval_handler=approval_handler, pipeline=pipeline
        )
        for entry in tools
    ]
