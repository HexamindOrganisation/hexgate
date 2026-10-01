"""The `guards:` policy block (R-GUARD-006).

Enable/disable a manifest-declared guard agent-wide, via the policy baseline (the
top-level `guards:`). v1 is baseline-only — a `guards:` on a tool / default_policy /
admission / reach entry is rejected loud (per-tool and per-caller governance is
deferred to v2). Guards are a build-time toggle, not an allow/deny decision, so they
are read via `AgentPolicy.effective_guards` and never lowered into `effective_tools`.
These tests pin the model shape, the placement validator, the baseline inheritance
merge, and that module composition rejects the block fail-loud in v1.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hexgate.security import (
    AgentPolicy,
    GuardRule,
    LinkError,
    ModuleContent,
    link,
)
from hexgate.security.policy_set import load_policy_set_from_dict

# --- GuardRule shape ----------------------------------------------------------


def test_guard_rule_requires_enabled() -> None:
    """A rule states an intent; there is no implied default."""
    with pytest.raises(ValidationError):
        GuardRule.model_validate({})


def test_guard_rule_rejects_unknown_key() -> None:
    """extra='forbid' catches a typo'd key rather than dropping it silently."""
    with pytest.raises(ValidationError):
        GuardRule.model_validate({"enable": True})


def test_guard_rule_rejects_params_in_v1() -> None:
    """v1 is enable/disable only — a param field is not part of the grammar yet."""
    with pytest.raises(ValidationError):
        GuardRule.model_validate({"enabled": True, "threshold": 5})


# --- effective_guards: baseline only (v1 is baseline-only, R-GUARD-006) -------


def test_baseline_applies_to_every_tool() -> None:
    policy = AgentPolicy(
        guards={"secret_guard": GuardRule(enabled=False)},
        tools={"send_email": {"mode": "allow"}},
    )
    # The baseline governs the agent, uniformly — the same stance for every tool.
    assert policy.effective_guards("send_email") == {"secret_guard": False}
    assert policy.effective_guards("unlisted_tool") == {"secret_guard": False}


def test_unmentioned_guard_is_absent_meaning_run_as_declared() -> None:
    """A guard the policy never names is absent from the stance (default: enabled)."""
    policy = AgentPolicy(tools={"send_email": {"mode": "allow"}})
    assert policy.effective_guards("send_email") == {}


def test_declares_guards() -> None:
    assert not AgentPolicy(tools={"t": {"mode": "allow"}}).declares_guards()
    assert AgentPolicy(guards={"g": GuardRule(enabled=False)}).declares_guards()


# --- placement validators -----------------------------------------------------


def test_guards_rejected_on_admission() -> None:
    with pytest.raises(ValidationError, match="guards"):
        AgentPolicy.model_validate(
            {"admission": {"mode": "allow", "guards": {"g": {"enabled": True}}}}
        )


def test_guards_rejected_on_default_policy() -> None:
    with pytest.raises(ValidationError, match="guards"):
        AgentPolicy.model_validate(
            {"default_policy": {"mode": "deny", "guards": {"g": {"enabled": True}}}}
        )


def test_guards_rejected_on_agents_entry() -> None:
    with pytest.raises(ValidationError, match="guards"):
        AgentPolicy.model_validate(
            {"agents": {"child": {"mode": "allow", "guards": {"g": {"enabled": True}}}}}
        )


def test_guards_rejected_on_skills_entry() -> None:
    with pytest.raises(ValidationError, match="guards"):
        AgentPolicy.model_validate(
            {
                "skills": {
                    "refunder": {"mode": "allow", "guards": {"g": {"enabled": True}}}
                }
            }
        )


def test_guards_allowed_on_baseline() -> None:
    """The one legal placement — the policy baseline — loads cleanly."""
    AgentPolicy.model_validate({"guards": {"secret_guard": {"enabled": False}}})


def test_per_tool_guards_rejected_in_v1() -> None:
    """A `guards:` inside a `tools:` entry is rejected loud — v1 governs guards only
    at the baseline; per-tool is deferred to v2 (R-GUARD-006)."""
    with pytest.raises(ValidationError, match="per-tool 'guards:' is not supported"):
        AgentPolicy.model_validate(
            {
                "tools": {
                    "send_email": {
                        "mode": "allow",
                        "guards": {"secret_guard": {"enabled": True}},
                    }
                }
            }
        )


# --- inheritance merge --------------------------------------------------------


def test_baseline_guards_inherit_and_child_overrides_per_key() -> None:
    ps = load_policy_set_from_dict(
        {
            "roles": {
                "base": {
                    "is_mixin": True,
                    "guards": {
                        "secret_guard": {"enabled": False},
                        "secret_watch": {"enabled": True},
                    },
                },
                "agent": {
                    "inherits": ["base"],
                    # Override one key; the other is inherited untouched.
                    "guards": {"secret_guard": {"enabled": True}},
                },
            }
        }
    )
    resolved = ps.policy_for("agent")
    assert resolved.effective_guards("any_tool") == {
        "secret_guard": True,  # child won
        "secret_watch": True,  # inherited
    }


# --- module composition (v1: not composable, rejected fail-loud) --------------


def test_module_composition_rejects_guards_block() -> None:
    """The fold composes tool decisions only; guards are not lowered into
    effective_tools, so a module setting `guards:` is rejected fail-loud (like
    `skills:`), never silently dropped. Deferred, see R-GUARD-006."""
    mod = ModuleContent(
        name="d",
        kind="boundary",
        policy=AgentPolicy(guards={"secret_guard": GuardRule(enabled=False)}),
        source="d.yaml",
        content_hash="hash-d",
    )
    with pytest.raises(LinkError, match=r"\['guards'\]"):
        link([mod], [])


def test_resolved_serializer_omits_empty_guards() -> None:
    """The modular resolve path never carries guards, so the resolved dump must not
    emit `guards: {}` — that would shift every stored bundle's source_hash for a
    field the resolved policy does not use (R-GUARD-006)."""
    from hexgate.security import effective_policy_by_role, resolve_for_project

    cap = ModuleContent(
        name="c",
        kind="capability",
        policy=AgentPolicy(tools={"x": {"mode": "allow"}}),
        source="c.yaml",
        content_hash="hash-c",
    )
    result = resolve_for_project([], [cap], {"default": ["c"]})
    dumped = effective_policy_by_role(result)["default"]
    assert "guards" not in dumped
    assert "guards" not in dumped["default_policy"]
    assert "guards" not in dumped["tools"]["x"]


def test_empty_guards_omitted_from_model_dump_everywhere() -> None:
    """The omission is intrinsic to the model, so a direct `model_dump` (the CLI's
    single-role / `--role` resolve paths) also emits no `guards: {}` — on the policy,
    default_policy, tools, admission, or agents/skills entries (R-GUARD-006). This is
    what keeps a guards-free policy's source_hash byte-identical on every dump path."""
    policy = AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "admission": {"mode": "allow"},
            "tools": {"send_email": {"mode": "allow"}},
            "agents": {"child": {"mode": "allow"}},
            "skills": {"refunder": {"mode": "allow"}},
        }
    )
    dumped = policy.model_dump(mode="json")
    assert "guards" not in dumped
    assert "guards" not in dumped["default_policy"]
    assert "guards" not in dumped["admission"]
    assert "guards" not in dumped["tools"]["send_email"]
    assert "guards" not in dumped["agents"]["child"]
    assert "guards" not in dumped["skills"]["refunder"]


def test_non_empty_guards_preserved_in_dump() -> None:
    """A real baseline rule is never hidden: a set guards map survives the dump."""
    policy = AgentPolicy.model_validate(
        {
            "guards": {"secret_guard": {"enabled": False}},
            "tools": {"send_email": {"mode": "allow"}},
        }
    )
    dumped = policy.model_dump(mode="json")
    assert dumped["guards"] == {"secret_guard": {"enabled": False}}
    assert "guards" not in dumped["tools"]["send_email"]
