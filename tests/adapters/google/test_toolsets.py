"""Tests for the Google ADK adapter policy gate on toolsets.

Covers #248: a ``BaseToolset`` in ``agent.tools`` must be gated, not rejected.

Uses a hand-rolled fake toolset rather than ADK's ``SkillToolset``, which only
exists from ADK 1.25.0 while ``pyproject.toml`` pins ``google-adk>=1.14``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.openapi.models import APIKey, APIKeyIn
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.auth.auth_tool import AuthConfig
from google.adk.models.llm_request import LlmRequest
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool

from hexgate.adapters.google.tools import GuardedToolset, wrap_tool, wrap_tools
from hexgate.security import AgentPolicy, PolicySet
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.security.policy_set import DEFAULT_ROLE_NAME
from tests.adapters.google.conftest import _FakeToolset

_INVOCATION_ID = "inv-1"


def _enforcer_for(spec: dict[str, Any]) -> PolicyEnforcer:
    return PolicyEnforcer(
        PolicySet({DEFAULT_ROLE_NAME: AgentPolicy.model_validate(spec)})
    )


def _allow_enforcer(tool_name: str = "echo") -> PolicyEnforcer:
    return _enforcer_for(
        {
            "default_policy": {"mode": "deny"},
            "tools": {tool_name: {"mode": "allow"}},
        }
    )


def _deny_enforcer() -> PolicyEnforcer:
    return _enforcer_for({"default_policy": {"mode": "deny"}})


def _make_function_tool(
    name: str = "echo", calls: list[str] | None = None
) -> FunctionTool:
    """A FunctionTool that appends to ``calls`` when its body actually runs."""

    def echo(text: str) -> str:
        """Echo the input back."""
        if calls is not None:
            calls.append(text)
        return f"echo:{text}"

    echo.__name__ = name
    return FunctionTool(func=echo)


class _DirectRegisteringToolset(_FakeToolset):
    """Mirrors ComputerUseToolset: writes its own raw tools into tools_dict.

    ``hidden`` are tools it registers but never returns from get_tools, so the
    gated copies can never displace them.
    """

    def __init__(
        self,
        tools: list[BaseTool],
        *,
        hidden: list[BaseTool] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(tools, **kwargs)
        self.hidden = hidden or []

    async def process_llm_request(self, *, tool_context: Any, llm_request: Any) -> None:
        await super().process_llm_request(
            tool_context=tool_context, llm_request=llm_request
        )
        for tool in self.tools + self.hidden:
            llm_request.tools_dict[tool.name] = tool


async def _denied(tool: BaseTool) -> bool:
    result = await tool.run_async(args={"text": "hi"}, tool_context=None)
    return "policy_denied" in str(result)


class _StubInvocationContext:
    """Carries the only attribute ``ReadonlyContext.invocation_id`` reads."""

    def __init__(self, invocation_id: str) -> None:
        self.invocation_id = invocation_id


def _readonly_context(invocation_id: str = _INVOCATION_ID) -> ReadonlyContext:
    return ReadonlyContext(_StubInvocationContext(invocation_id))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# wrap_tools routing
# ---------------------------------------------------------------------------


def test_wrap_tools_wraps_a_toolset_instead_of_raising() -> None:
    """The #248 regression: a toolset entry is gated, not rejected."""
    toolset = _FakeToolset([_make_function_tool()])

    wrapped = wrap_tools([toolset], _allow_enforcer())

    assert len(wrapped) == 1
    assert isinstance(wrapped[0], GuardedToolset)


def test_wrap_tools_accepts_mixed_tools_and_toolsets() -> None:
    """A mixed list wraps elementwise, preserving order."""
    entries = [
        _make_function_tool("a"),
        _FakeToolset([_make_function_tool("b")]),
        _make_function_tool("c"),
    ]

    wrapped = wrap_tools(entries, _allow_enforcer())

    assert [isinstance(w, GuardedToolset) for w in wrapped] == [False, True, False]
    assert wrapped[0].name == "a"
    assert wrapped[2].name == "c"


# ---------------------------------------------------------------------------
# Gating of the yielded tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guarded_toolset_denies_and_does_not_execute() -> None:
    """A denied tool returns the marker and its body never runs."""
    calls: list[str] = []
    guarded = GuardedToolset(
        _FakeToolset([_make_function_tool(calls=calls)]), _deny_enforcer()
    )

    tools = await guarded.get_tools_with_prefix(None)
    result = await tools[0].run_async(args={"text": "hi"}, tool_context=None)

    assert "policy_denied" in result
    assert calls == []


@pytest.mark.asyncio
async def test_guarded_toolset_allows_permitted_tool() -> None:
    """An allowed tool executes and returns its real result."""
    calls: list[str] = []
    guarded = GuardedToolset(
        _FakeToolset([_make_function_tool(calls=calls)]), _allow_enforcer()
    )

    tools = await guarded.get_tools_with_prefix(None)
    result = await tools[0].run_async(args={"text": "hi"}, tool_context=None)

    assert result == "echo:hi"
    assert calls == ["hi"]


# ---------------------------------------------------------------------------
# Delegation to the inner toolset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guarded_toolset_delegates_process_llm_request() -> None:
    """The inner still sees the request — losing this silently loses skills."""
    inner = _FakeToolset([_make_function_tool()])
    guarded = GuardedToolset(inner, _allow_enforcer())
    request = LlmRequest()

    await guarded.process_llm_request(tool_context=None, llm_request=request)

    assert inner.llm_requests == [request]


@pytest.mark.asyncio
async def test_guarded_toolset_delegates_close() -> None:
    """Closing the wrapper releases the inner's resources."""
    inner = _FakeToolset([_make_function_tool()])

    await GuardedToolset(inner, _allow_enforcer()).close()

    assert inner.closed is True


def test_guarded_toolset_delegates_get_auth_config() -> None:
    """Credentials flow through the wrapper rather than defaulting to None."""
    config = AuthConfig(auth_scheme=APIKey(**{"in": APIKeyIn.header}, name="X-Api-Key"))
    inner = _FakeToolset([_make_function_tool()], auth_config=config)

    assert GuardedToolset(inner, _allow_enforcer()).get_auth_config() is config


# ---------------------------------------------------------------------------
# Re-gating and prefixes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guarded_toolset_regates_tools_added_later() -> None:
    """Tools a toolset exposes only later in a run come back gated too."""
    inner = _FakeToolset([_make_function_tool("echo")], dynamic=True)
    guarded = GuardedToolset(inner, _allow_enforcer("echo"))

    first = await guarded.get_tools_with_prefix(None)
    inner.tools.append(_make_function_tool("later"))
    second = await guarded.get_tools_with_prefix(None)

    assert [t.name for t in first] == ["echo"]
    assert [t.name for t in second] == ["echo", "later"]
    denied = await second[1].run_async(args={"text": "hi"}, tool_context=None)
    assert "policy_denied" in denied


@pytest.mark.asyncio
async def test_guarded_toolset_preserves_inner_tool_name_prefix() -> None:
    """The inner applies its own prefix; the wrapper does not drop it."""
    inner = _FakeToolset([_make_function_tool()], tool_name_prefix="mymcp")
    guarded = GuardedToolset(inner, _allow_enforcer())

    tools = await guarded.get_tools_with_prefix(None)

    assert [t.name for t in tools] == ["mymcp_echo"]


@pytest.mark.asyncio
async def test_guarded_toolset_gates_on_the_prefixed_name() -> None:
    """The gate keys on the name the model calls, not the inner's bare name."""
    prefixed = _FakeToolset([_make_function_tool()], tool_name_prefix="mymcp")
    bare = _FakeToolset([_make_function_tool()], tool_name_prefix="mymcp")

    allowed_tools = await GuardedToolset(
        prefixed, _allow_enforcer("mymcp_echo")
    ).get_tools_with_prefix(None)
    denied_tools = await GuardedToolset(
        bare, _allow_enforcer("echo")
    ).get_tools_with_prefix(None)

    allowed = await allowed_tools[0].run_async(args={"text": "hi"}, tool_context=None)
    denied = await denied_tools[0].run_async(args={"text": "hi"}, tool_context=None)

    assert allowed == "echo:hi"
    assert "policy_denied" in denied


@pytest.mark.asyncio
async def test_guarded_toolset_does_not_cache_within_an_invocation() -> None:
    """The wrapper must not memoize: the inner owns caching."""
    inner = _FakeToolset([_make_function_tool("echo")], dynamic=True)
    guarded = GuardedToolset(inner, _allow_enforcer("echo"))
    context = _readonly_context()

    first = await guarded.get_tools_with_prefix(context)
    inner.tools.append(_make_function_tool("later"))
    second = await guarded.get_tools_with_prefix(context)

    assert [t.name for t in first] == ["echo"]
    assert [t.name for t in second] == ["echo", "later"]
    denied = await second[1].run_async(args={"text": "hi"}, tool_context=None)
    assert "policy_denied" in denied


@pytest.mark.asyncio
async def test_guarded_toolset_get_tools_does_not_raise_on_empty_inner() -> None:
    """ADK swallows toolset errors, so the wrapper must not rely on raising."""
    guarded = GuardedToolset(_FakeToolset([]), _allow_enforcer())

    assert await guarded.get_tools_with_prefix(None) == []


def test_wrap_tool_still_rejects_a_toolset_directly() -> None:
    """wrap_tool keeps its clear error: only wrap_tools routes toolsets."""
    with pytest.raises(TypeError, match="BaseTool or callable"):
        wrap_tool(_FakeToolset([]), _allow_enforcer())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tools the inner registers into tools_dict itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guarded_toolset_gates_directly_registered_tools_under_a_prefix() -> None:
    """A prefixing toolset registers raw tools under their BARE names, which the
    prefixed gated copies never displace — so dispatch would find them ungated."""
    inner = _DirectRegisteringToolset([_make_function_tool()], tool_name_prefix="mymcp")
    guarded = GuardedToolset(inner, _deny_enforcer())
    request = LlmRequest()

    await guarded.process_llm_request(tool_context=None, llm_request=request)

    assert await _denied(request.tools_dict["echo"])


@pytest.mark.asyncio
async def test_guarded_toolset_gates_tools_absent_from_get_tools() -> None:
    """A tool the inner registers but never returns from get_tools still runs
    through the gate — nothing else would ever wrap it."""
    inner = _DirectRegisteringToolset(
        [_make_function_tool()], hidden=[_make_function_tool("hidden")]
    )
    guarded = GuardedToolset(inner, _deny_enforcer())
    request = LlmRequest()

    await guarded.process_llm_request(tool_context=None, llm_request=request)

    assert await _denied(request.tools_dict["hidden"])
    assert await _denied(request.tools_dict["echo"])


@pytest.mark.asyncio
async def test_guarded_toolset_leaves_tools_it_did_not_register_untouched() -> None:
    """Only entries the inner added or replaced are re-gated; an unrelated tool
    already in the dict is left exactly as it was, never double-wrapped."""
    untouched = _make_function_tool("untouched")
    request = LlmRequest()
    request.tools_dict["untouched"] = untouched
    guarded = GuardedToolset(
        _DirectRegisteringToolset([_make_function_tool()]), _deny_enforcer()
    )

    await guarded.process_llm_request(tool_context=None, llm_request=request)

    assert request.tools_dict["untouched"] is untouched
