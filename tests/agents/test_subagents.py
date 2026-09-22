"""Tests for the agent-as-tool construct — native sub-agent reach.

Agent-as-tool: ``create_agent(tools=[child.as_tool()])`` mounts a child
``HexgateAgent`` as a gated delegation tool that decides ``agent.tool:<child>``
under the PARENT's policy (via the reach gate), then runs the child's own
``ainvoke`` under the inherited role. It is the only native reach mode (handoff
has no native seam). The behavioral tests drive the seam directly (no live LLM):
a fake child records the ambient role and echoes, so we can assert gating +
propagation.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from hexgate.adapters.langchain.tools import SubagentTool
from hexgate.agents import factory
from hexgate.agents.subagents import SubagentEdge
from hexgate.runtime.context import HexgateContext, get_current_context
from hexgate.security import AgentPolicy, BaseToolPolicy
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.security.policy_set import load_policy_set

_ROLE = HexgateContext(user_id="u", user_roles=["support"])


class _FakeChild:
    """A HexgateAgent stand-in: a name + an async ``ainvoke`` that records the
    ambient role (to prove propagation) and echoes the delegated task."""

    def __init__(self, name: str = "billing_bot") -> None:
        self.name = name
        self.seen_roles: list[str] | None = None

    async def ainvoke(self, payload: dict, config: dict) -> dict:
        ctx = get_current_context()
        self.seen_roles = list(ctx.user_roles) if ctx else None
        task = payload["messages"][-1]["content"]
        return {"messages": [AIMessage(content=f"child handled: {task}")]}


def _enforcer(agents: dict) -> PolicyEnforcer:
    policy = AgentPolicy(default_policy=BaseToolPolicy(mode="allow"), agents=agents)
    return PolicyEnforcer(load_policy_set(policy), agent_name="parent")


def _tool(child: _FakeChild, enforcer: PolicyEnforcer | None = None) -> SubagentTool:
    return SubagentTool(
        name="delegate",
        description="d",
        child=child,
        target_name=child.name,
        enforcer=enforcer,
    )


def _hg(name: str):
    """A real (hermetic) HexgateAgent, so its ``.as_tool()`` method is available."""
    agent, _ = factory.create_agent("m", tools=[], name=name)
    return agent


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the graph build + handler and clear governance env for construction."""
    monkeypatch.setattr(
        factory, "create_langchain_agent", lambda **kwargs: "graph-instance"
    )
    monkeypatch.setattr(
        factory, "get_langfuse_handler", lambda **kwargs: "handler-instance"
    )
    for var in (
        "HEXGATE_API_KEY",
        "HEXGATE_LOCAL_POLICY",
        "HEXGATE_BIND_AGENTS",
        "HEXGATE_LOCAL_MODE",
    ):
        monkeypatch.delenv(var, raising=False)


# --- construction -----------------------------------------------------------


def test_create_agent_mounts_subagent_as_tool() -> None:
    billing = _hg("billing_bot")
    parent, _ = factory.create_agent("m", tools=[billing.as_tool()], name="parent")
    delegation = [t for t in parent.tools if isinstance(t, SubagentTool)]
    assert [t.name for t in delegation] == ["delegate_to_billing_bot"]
    assert delegation[0].target_name == "billing_bot"
    # The subagents property derives the tool edge from the mounted SubagentTool.
    assert parent.subagents == [
        SubagentEdge(child=billing, via="tool", as_name="delegate_to_billing_bot")
    ]


def test_as_tool_defaults_and_overrides() -> None:
    billing = _hg("billing_bot")
    st = billing.as_tool()
    assert isinstance(st, SubagentTool)
    assert st.name == "delegate_to_billing_bot"
    assert st.target_name == "billing_bot"
    assert st.child is billing
    st2 = billing.as_tool(name="refund_it", description="Refund via billing.")
    assert st2.name == "refund_it"
    assert st2.description == "Refund via billing."
    assert st2.target_name == "billing_bot"  # reach key keeps the canonical name


def test_nameless_agent_as_tool_raises() -> None:
    nameless = _hg("")
    with pytest.raises(ValueError, match="must have a name"):
        nameless.as_tool()


def test_as_tool_invalid_name_override_raises() -> None:
    # A `name` override is used verbatim, so an invalid provider tool name must fail
    # loud here — not at the parent's first tool call with an opaque provider error.
    billing = _hg("billing_bot")
    with pytest.raises(ValueError, match="valid tool name"):
        billing.as_tool(name="refund it")  # space is outside ^[A-Za-z0-9_-]$


# --- reach gating at the seam ----------------------------------------------


@pytest.mark.asyncio
async def test_reach_allow_runs_child_with_role() -> None:
    child = _FakeChild()
    tool = _tool(child, _enforcer({"billing_bot": {"mode": "allow", "via": ["tool"]}}))
    async with _ROLE:
        out = await tool._arun("refund it")
    assert out == "child handled: refund it"
    assert child.seen_roles == ["support"]  # caller's role propagated into the child


@pytest.mark.asyncio
async def test_reach_deny_renders_error_and_skips_child() -> None:
    # The policy declares agent-as-tool reach (so the reach key is decided), and
    # billing_bot's tool reach is denied → agent.tool:billing_bot denied, child skipped.
    child = _FakeChild()
    tool = _tool(child, _enforcer({"billing_bot": {"mode": "deny", "via": ["tool"]}}))
    async with _ROLE:
        out = await tool._arun("refund it")
    assert isinstance(out, dict) and out["ok"] is False  # structured governance error
    assert child.seen_roles is None  # the child never ran


@pytest.mark.asyncio
async def test_handoff_only_policy_name_gates_the_delegation() -> None:
    # billing_bot is granted handoff-only (no via:tool declared anywhere), so the
    # policy declares no agent-as-tool reach → the delegation is gated by the plain
    # tool name against the default policy (allow here), mirroring the OpenAI adapter
    # (a handoff-only policy does not closed-world-deny every as-tool with no signal).
    child = _FakeChild()
    tool = _tool(
        child, _enforcer({"billing_bot": {"mode": "allow", "via": ["handoff"]}})
    )
    async with _ROLE:
        out = await tool._arun("refund it")
    assert out == "child handled: refund it"  # allow-default name-gate → runs


@pytest.mark.asyncio
async def test_no_reach_block_is_a_noop() -> None:
    child = _FakeChild()
    tool = _tool(child, _enforcer({}))  # no agents block → reach gate no-op
    async with _ROLE:
        out = await tool._arun("hi")
    assert out == "child handled: hi"


@pytest.mark.asyncio
async def test_unbound_tool_skips_reach_but_runs_child() -> None:
    # enforcer=None (agent built without policy) → reach skipped; child self-enforces.
    child = _FakeChild()
    tool = _tool(child, enforcer=None)
    async with _ROLE:
        out = await tool._arun("hi")
    assert out == "child handled: hi"


def test_sync_run_is_unsupported() -> None:
    with pytest.raises(NotImplementedError):
        _tool(_FakeChild())._run("hi")


# --- robustness -------------------------------------------------------------


class _StubDecision:
    agent_name = "billing_bot"

    def as_error_payload(self) -> dict:
        return {"reason": "admission denied"}


async def test_child_governance_refusal_renders_error() -> None:
    # Reach allowed, but the child refuses the inherited caller (admission) — render
    # as a governance tool error, not a raw exception out of _arun.
    from hexgate.security.agent_gate import AgentNotAdmittedError

    class _Denied:
        name = "billing_bot"

        async def ainvoke(self, payload: dict, config: dict) -> dict:
            raise AgentNotAdmittedError(_StubDecision())

    tool = _tool(
        _Denied(), _enforcer({"billing_bot": {"mode": "allow", "via": ["tool"]}})
    )
    async with _ROLE:
        out = await tool._arun("hi")
    assert isinstance(out, dict) and out["ok"] is False


async def test_empty_and_multiblock_child_results() -> None:
    class _Empty:
        name = "billing_bot"

        async def ainvoke(self, payload: dict, config: dict) -> dict:
            return {"messages": []}

    class _Blocks:
        name = "billing_bot"

        async def ainvoke(self, payload: dict, config: dict) -> dict:
            content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
            return {"messages": [AIMessage(content=content)]}

    async with _ROLE:
        assert await _tool(_Empty())._arun("x") == ""  # no message → empty string
        assert await _tool(_Blocks())._arun("x") == "ab"  # blocks flattened to text


def test_duplicate_subagent_names_raise() -> None:
    # Two agents named the same → identical default as_tool() names → name collision.
    a, b = _hg("billing_bot"), _hg("billing_bot")
    with pytest.raises(ValueError, match="collides"):
        factory.create_agent("m", tools=[a.as_tool(), b.as_tool()], name="parent")


def test_delegation_name_collides_with_existing_tool() -> None:
    from langchain_core.tools import tool as lc_tool

    @lc_tool
    def delegate_to_billing_bot(x: str) -> str:
        """An existing tool that shadows the generated delegation name."""
        return x

    billing = _hg("billing_bot")
    with pytest.raises(ValueError, match="collides"):
        factory.create_agent(
            "m", tools=[delegate_to_billing_bot, billing.as_tool()], name="parent"
        )


def test_duplicate_target_under_distinct_tool_names_raises() -> None:
    # Distinct tool names but the same target agent → one agent.tool:billing_bot key.
    a, b = _hg("billing_bot"), _hg("billing_bot")
    with pytest.raises(ValueError, match="collapse onto one agent.tool"):
        factory.create_agent(
            "m",
            tools=[a.as_tool(name="delegate_a"), b.as_tool(name="delegate_b")],
            name="parent",
        )


def test_with_tools_rechecks_subagent_collisions() -> None:
    # The collision guard must fire on the with_tools rebuild path too, not only in
    # create_agent — else a colliding SubagentTool slips in via the lower-level API.
    parent = _hg("support_bot")
    a, b = _hg("billing_bot"), _hg("billing_bot")
    with pytest.raises(ValueError, match="collapse onto one agent.tool"):
        parent.with_tools([a.as_tool(name="delegate_a"), b.as_tool(name="delegate_b")])


def test_delegation_collides_with_plain_callable_tool() -> None:
    def delegate_to_billing_bot(x: str) -> str:  # a raw callable, not a BaseTool
        return x

    billing = _hg("billing_bot")
    with pytest.raises(ValueError, match="collides"):
        factory.create_agent(
            "m", tools=[delegate_to_billing_bot, billing.as_tool()], name="parent"
        )


async def test_structured_result_is_not_dropped() -> None:
    class _Structured:
        name = "billing_bot"

        async def ainvoke(self, payload: dict, config: dict) -> dict:
            return {"result": {"refunded": 40}}  # response_format shape, no messages

    async with _ROLE:
        out = await _tool(_Structured())._arun("x")
    # Conveyed as clean JSON (parseable by the parent LLM), not a Python repr or "".
    import json

    assert json.loads(out) == {"result": {"refunded": 40}}


async def test_dict_final_message_reads_content() -> None:
    class _DictMsg:
        name = "billing_bot"

        async def ainvoke(self, payload: dict, config: dict) -> dict:
            return {"messages": [{"role": "assistant", "content": "done"}]}

    async with _ROLE:
        out = await _tool(_DictMsg())._arun("x")
    assert out == "done"  # reads last["content"], not str(dict)


async def test_structured_response_preferred_over_messages() -> None:
    # A response_format child returns BOTH messages and structured_response — the
    # structured payload is the real answer and must not be shadowed by the message.
    class _Formatted:
        name = "billing_bot"

        async def ainvoke(self, payload: dict, config: dict) -> dict:
            return {
                "messages": [AIMessage(content="chatter")],
                "structured_response": {"refunded": 40},
            }

    async with _ROLE:
        out = await _tool(_Formatted())._arun("x")
    # Structured answer wins over "chatter", serialized to text (a ToolNode needs a
    # string tool-message content — never a raw dict).
    assert isinstance(out, str)
    import json

    assert json.loads(out) == {"refunded": 40}


def test_delegation_collides_with_function_style_dict_tool() -> None:
    fn_tool = {"type": "function", "function": {"name": "delegate_to_billing_bot"}}
    billing = _hg("billing_bot")
    with pytest.raises(ValueError, match="collides"):
        factory.create_agent("m", tools=[fn_tool, billing.as_tool()], name="parent")


def test_whitespace_only_agent_as_tool_raises() -> None:
    # A whitespace-only name canonicalizes to "default"; fail loud in as_tool()
    # instead of silently mounting under agent.tool:default with a malformed name.
    nameless = _hg(" ")
    with pytest.raises(ValueError, match="must have a name"):
        nameless.as_tool()


def test_names_that_canonicalize_identically_collide() -> None:
    # "billing_bot" and " billing_bot " canonicalize to the same reach key. Distinct
    # as_tool names dodge the name check, so the *target* guard must still catch the
    # collapse onto one agent.tool:billing_bot key (it compares canonical names).
    a, b = _hg("billing_bot"), _hg(" billing_bot ")
    with pytest.raises(ValueError, match="collapse onto one agent.tool"):
        factory.create_agent(
            "m",
            tools=[a.as_tool(name="delegate_a"), b.as_tool(name="delegate_b")],
            name="parent",
        )


def test_spaced_child_name_yields_valid_tool_name() -> None:
    import re

    # A name with a space is a valid reach target but an INVALID LLM tool name; the
    # generated tool name is sanitized while the reach target keeps the canonical name.
    billing = _hg("Billing Bot")
    st = billing.as_tool()
    assert st.name == "delegate_to_Billing_Bot"
    assert re.fullmatch(r"[A-Za-z0-9_-]+", st.name)  # provider-valid
    assert st.target_name == "Billing Bot"  # reach key keeps the canonical target


async def test_none_content_message_yields_empty_string() -> None:
    # A tool-call-only / empty completion has content=None → "", not the word "None".
    class _NoneContent:
        name = "billing_bot"

        async def ainvoke(self, payload: dict, config: dict) -> dict:
            return {"messages": [{"role": "assistant", "content": None}]}

    async with _ROLE:
        out = await _tool(_NoneContent())._arun("x")
    assert out == ""


# --- guards + coverage-aware warning ---------------------------------------


def _halt_guard():
    from hexgate.guards import before_tool
    from hexgate.guards.types import Halt

    return before_tool(lambda call: Halt(reason="blocked"))


def test_guards_thread_into_subagent_tool() -> None:
    parent, _ = factory.create_agent(
        "m", tools=[_tool(_FakeChild())], name="parent", guards=[_halt_guard()]
    )
    st = next(t for t in parent.tools if isinstance(t, SubagentTool))
    assert st.pipeline is not None and not st.pipeline.is_empty


async def test_guard_halt_blocks_delegation() -> None:
    child = _FakeChild()
    parent, _ = factory.create_agent(
        "m", tools=[_tool(child)], name="parent", guards=[_halt_guard()]
    )
    st = next(t for t in parent.tools if isinstance(t, SubagentTool))
    async with _ROLE:
        out = await st._arun("hi")
    assert isinstance(out, dict) and out["ok"] is False  # guard halted the delegation
    assert child.seen_roles is None  # child never ran


async def test_guards_survive_reenforce_on_subagent_tool() -> None:
    # A guard pipeline mounted at create_agent(guards=[...]) must survive a later
    # enforce_policy(policy) that supplies no guards — the SubagentTool falls through
    # like GuardedTool.wrap instead of overwriting pipeline with None.
    child = _FakeChild()
    parent, _ = factory.create_agent(
        "m", tools=[_tool(child)], name="parent", guards=[_halt_guard()]
    )
    policy = AgentPolicy(
        default_policy=BaseToolPolicy(mode="allow"),
        agents={"billing_bot": {"mode": "allow", "via": ["tool"]}},
    )
    rebound = parent.enforce_policy(policy)  # no guards re-supplied
    st = next(t for t in rebound.tools if isinstance(t, SubagentTool))
    assert st.pipeline is not None and not st.pipeline.is_empty  # pipeline not wiped
    async with _ROLE:
        out = await st._arun("hi")
    assert isinstance(out, dict) and out["ok"] is False  # guard still halts
    assert child.seen_roles is None  # child never ran


async def test_reach_deny_is_recorded_and_skips_child() -> None:
    # A reach-denied delegation is decided by the shared runner: recorded as a DENIAL
    # (not a successful tool call) and the child never runs. Guards precede the policy
    # decision (as on every adapter), but a denial still short-circuits execution.
    from hexgate.guards import before_tool
    from hexgate.guards.types import build_pipeline
    from hexgate.runtime.run_facts import run_scope

    ran: list[str] = []
    pipeline = build_pipeline([before_tool(lambda call: ran.append(call.tool_name))])
    child = _FakeChild()
    tool = SubagentTool(
        name="delegate",
        description="d",
        child=child,
        target_name=child.name,
        enforcer=_enforcer({"billing_bot": {"mode": "deny", "via": ["tool"]}}),
        pipeline=pipeline,
    )
    async with _ROLE:
        with run_scope("r") as facts:
            out = await tool._arun("hi")
    assert isinstance(out, dict) and out["ok"] is False  # reach denied
    assert child.seen_roles is None  # child never ran
    assert facts.denials == 1  # recorded as a denial...
    assert facts.tool_calls == 0  # ...not as an executed tool call


async def test_no_agents_block_deny_default_still_gates_the_delegation() -> None:
    # The fail-open guard: a deny-default agent with NO `agents:` block mounts a
    # sub-agent as a tool. Reach doesn't engage (no reach declared), so the delegation
    # must fall back to the plain tool-name policy — deny-default → denied, child
    # never runs, recorded as a denial. Not a silent bypass.
    from hexgate.runtime.run_facts import run_scope

    child = _FakeChild()
    deny_default = AgentPolicy(default_policy=BaseToolPolicy(mode="deny"), agents={})
    enforcer = PolicyEnforcer(load_policy_set(deny_default), agent_name="parent")
    tool = SubagentTool(
        name="delegate_to_billing_bot",
        description="d",
        child=child,
        target_name="billing_bot",
        enforcer=enforcer,
    )
    async with _ROLE:
        with run_scope("r") as facts:
            out = await tool._arun("refund it")
    assert isinstance(out, dict) and out["ok"] is False  # denied by default policy
    assert child.seen_roles is None  # child never ran
    assert facts.denials == 1  # audited, not a silent pass-through


async def test_child_refusal_in_pipeline_is_recorded_as_error_not_success() -> None:
    # Reach is allowed, but the child refuses (admission). With a guard pipeline the
    # refusal must count as an ERROR in the run facts, not a successful tool call —
    # otherwise the blocked delegation is audited as ToolOutcome ok=True.
    from hexgate.guards import before_tool
    from hexgate.guards.types import build_pipeline
    from hexgate.runtime.run_facts import run_scope
    from hexgate.security.agent_gate import AgentNotAdmittedError

    class _Denied:
        name = "billing_bot"

        async def ainvoke(self, payload: dict, config: dict) -> dict:
            raise AgentNotAdmittedError(_StubDecision())

    seen: list[str] = []
    pipeline = build_pipeline([before_tool(lambda call: seen.append(call.tool_name))])
    tool = SubagentTool(
        name="delegate",
        description="d",
        child=_Denied(),
        target_name="billing_bot",
        enforcer=_enforcer({"billing_bot": {"mode": "allow", "via": ["tool"]}}),
        pipeline=pipeline,
    )
    async with _ROLE:
        with run_scope("r") as facts:
            out = await tool._arun("hi")
    assert isinstance(out, dict) and out["ok"] is False  # still rendered as tool output
    assert seen == ["delegate"]  # pipeline ran (reach allowed)
    assert facts.errors == 1  # counted as an error, NOT a clean success
    assert facts.denials == 0


def test_reach_warning_is_coverage_aware(caplog) -> None:
    import logging

    parent, _ = factory.create_agent(
        "m", tools=[_tool(_FakeChild("billing_bot"))], name="cov_parent"
    )
    covered = AgentPolicy(
        default_policy=BaseToolPolicy(mode="allow"),
        agents={"billing_bot": {"mode": "allow", "via": ["tool"]}},
    )
    with caplog.at_level(logging.WARNING):
        parent.enforce_policy(covered)
    assert "reach" not in caplog.text.lower()  # fully covered → quiet
    caplog.clear()
    # Handoff declared but no native seam → warn.
    handoff = AgentPolicy(
        default_policy=BaseToolPolicy(mode="allow"),
        agents={"billing_bot": {"mode": "allow", "via": ["handoff"]}},
    )
    with caplog.at_level(logging.WARNING):
        parent.enforce_policy(handoff)
    assert "reach" in caplog.text.lower()


def test_default_via_grant_with_as_tool_is_quiet(caplog) -> None:
    import logging

    # The idiomatic minimal grant: no explicit `via`, so it defaults to
    # ["tool", "handoff"]. With the child mounted as a tool, the handoff bit is a
    # permissive-default artifact, not an unenforced edge — coverage stays quiet.
    parent, _ = factory.create_agent(
        "m", tools=[_tool(_FakeChild("billing_bot"))], name="default_via_parent"
    )
    policy = AgentPolicy(
        default_policy=BaseToolPolicy(mode="allow"),
        agents={"billing_bot": {"mode": "allow"}},  # via defaults to tool + handoff
    )
    with caplog.at_level(logging.WARNING):
        parent.enforce_policy(policy)
    assert "reach" not in caplog.text.lower()  # tool-mounted+granted → no spurious warn


def test_coverage_uses_canonical_child_name(caplog) -> None:
    import logging

    # A non-canonical child name (surrounding whitespace) canonicalizes to the same
    # key the policy authors — coverage must match it, not warn spuriously.
    parent, _ = factory.create_agent(
        "m", tools=[_tool(_FakeChild(" billing_bot "))], name="canon_parent"
    )
    covered = AgentPolicy(
        default_policy=BaseToolPolicy(mode="allow"),
        agents={"billing_bot": {"mode": "allow", "via": ["tool"]}},
    )
    with caplog.at_level(logging.WARNING):
        parent.enforce_policy(covered)
    assert "reach" not in caplog.text.lower()  # canonical match → quiet
