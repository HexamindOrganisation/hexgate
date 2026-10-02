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

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from hexgate.guards import ToolCall, before_tool, build_pipeline
from hexgate.guards.stance import GuardClosedWorldError
from hexgate.security import (
    AgentPolicy,
    GuardRule,
    LinkError,
    ModuleContent,
    link,
    lint_guards,
)
from hexgate.security.bundle import build_signed_bundle
from hexgate.security.policy_set import PolicySetError, load_policy_set_from_dict

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


# --- PR3: bundle carriage + stance projection + filter (R-GUARD-007) -----------


def test_guard_stance_single_role() -> None:
    ps = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"secret_guard": {"enabled": False}}}}}
    )
    assert ps.guard_stance() == {"baseline": {"secret_guard": False}}
    assert ps.effective_guards("any") == {"secret_guard": False}
    assert ps.governed_guard_names() == frozenset({"secret_guard"})


def test_guard_stance_none_when_no_guards() -> None:
    ps = load_policy_set_from_dict(
        {"roles": {"default": {"tools": {"t": {"mode": "allow"}}}}}
    )
    assert ps.guard_stance() is None
    assert ps.effective_guards("t") == {}
    assert ps.governed_guard_names() == frozenset()


def test_compose_entry_file_guards_reach_the_runtime_stance() -> None:
    """A `guards:` block authored in the compose `policy.yaml` entry file (what the
    dashboard edits) resolves to the same agent-level stance the runtime reads — the
    full compose -> AgentPolicy.guards -> guard_stance path (R-GUARD-006/007). It is
    agent-level, so a multi-role agent resolves to one stance, no divergence."""
    from hexgate.security.compose import resolve_text

    ps = resolve_text(
        "version: 1\n"
        "guards: { secret_guard: { enabled: false } }\n"
        "agents:\n"
        "  bot:\n"
        "    roles:\n"
        "      default: { tools: { a: { mode: allow } } }\n"
        "      admin:   { tools: { a: { mode: allow } } }\n",
        agent="bot",
    ).policy_set

    assert sorted(ps.roles) == ["admin", "default"]
    assert ps.guard_stance() == {"baseline": {"secret_guard": False}}
    assert ps.effective_guards("a") == {"secret_guard": False}
    assert ps.governed_guard_names() == frozenset({"secret_guard"})


def test_guard_stance_multi_role_agree() -> None:
    shared = {"guards": {"secret_guard": {"enabled": False}}}
    ps = load_policy_set_from_dict(
        {
            "roles": {
                "base": {"is_mixin": True, **shared},
                "support": {"inherits": ["base"]},
                "admin": {"inherits": ["base"]},
            }
        }
    )
    assert ps.guard_stance() == {"baseline": {"secret_guard": False}}


def test_guard_stance_multi_role_diverge_raises() -> None:
    ps = load_policy_set_from_dict(
        {
            "roles": {
                "support": {"guards": {"secret_guard": {"enabled": False}}},
                "admin": {"guards": {"secret_guard": {"enabled": True}}},
            }
        }
    )
    with pytest.raises(PolicySetError, match="same stance across all roles"):
        ps.guard_stance()


def test_guard_stance_caches_and_reraises_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A divergent stance is computed once: the PolicySetError is cached and re-raised,
    so a live divergent fallback engine does not recompute (and re-log) every guarded
    call (review #5)."""
    from hexgate.security import policy_set as ps_mod

    ps = load_policy_set_from_dict(
        {
            "roles": {
                "support": {"guards": {"secret_guard": {"enabled": False}}},
                "admin": {"guards": {"secret_guard": {"enabled": True}}},
            }
        }
    )
    calls = {"n": 0}
    real = ps_mod.PolicySet._compute_guard_stance

    def _counting(self: object) -> object:
        calls["n"] += 1
        return real(self)

    monkeypatch.setattr(ps_mod.PolicySet, "_compute_guard_stance", _counting)

    with pytest.raises(PolicySetError):
        ps.guard_stance()
    with pytest.raises(PolicySetError):
        ps.guard_stance()
    assert calls["n"] == 1  # second call re-raises the cached error, no recompute


def test_bundle_carries_and_reads_guard_stance() -> None:
    from hexgate.security.bundle import PolicyBundle

    yaml_text = "guards:\n  secret_guard: {enabled: false}\ntools:\n  send_email:\n    mode: allow\n"
    bundle = build_signed_bundle(yaml_text, compile_wasm=False)
    assert bundle.manifest["guards"] == {"baseline": {"secret_guard": False}}
    pb = PolicyBundle(
        source_path=None,
        rego_text=bundle.rego_text,
        wasm_bytes=bundle.wasm_bytes,
        manifest=bundle.manifest,
    )
    # Baseline-only: the stance is uniform for every tool.
    assert pb.effective_guards("send_email") == {"secret_guard": False}
    assert pb.effective_guards("other") == {"secret_guard": False}
    assert pb.governed_guard_names() == frozenset({"secret_guard"})


def test_bundle_omits_empty_guard_stance() -> None:
    """A guards-free policy carries no `guards` manifest key, so its signed bytes do
    not move (R-GUARD-007)."""
    from hexgate.security.bundle import PolicyBundle

    bundle = build_signed_bundle("tools:\n  t: {mode: allow}\n", compile_wasm=False)
    assert "guards" not in bundle.manifest
    pb = PolicyBundle(
        source_path=None,
        rego_text=bundle.rego_text,
        wasm_bytes=bundle.wasm_bytes,
        manifest=bundle.manifest,
    )
    assert pb.effective_guards("t") == {}  # default-safe: everything enabled
    assert pb.governed_guard_names() == frozenset()


def _named_before(label: str):
    def _fn(call: ToolCall) -> None:
        return None

    _fn.__name__ = label
    return before_tool(_fn)


# --- The stance is applied per call in the runner (R-GUARD-007) ---------------


def _recording_guard(label: str, fired: list[str]):
    def _fn(call: ToolCall) -> None:
        fired.append(label)
        return None

    _fn.__name__ = label
    return before_tool(_fn)


def _run(pipeline, engine, tool_name: str) -> None:
    from hexgate.guards.runner import run_guarded_sync
    from hexgate.security.enforcer import build_enforcer

    enforcer = build_enforcer(engine, agent_name="a")
    run_guarded_sync(
        tool_name,
        {},
        enforcer=enforcer,
        pipeline=pipeline,
        approval_handler=None,
        invoke=lambda final: "ok",
        render_error=lambda decision: "denied",
    )


def test_disabled_guard_is_skipped_at_runtime() -> None:
    fired: list[str] = []
    g_keep = _recording_guard("g_keep", fired)
    g_drop = _recording_guard("g_drop", fired)
    pipeline = build_pipeline([g_keep, g_drop])
    engine = load_policy_set_from_dict(
        {
            "roles": {
                "default": {
                    "tools": {"send_email": {"mode": "allow"}},
                    "guards": {"g_drop": {"enabled": False}},
                }
            }
        }
    )
    _run(pipeline, engine, "send_email")
    assert fired == ["g_keep"]  # g_drop skipped, g_keep ran


def test_unmentioned_guard_runs_by_default_at_runtime() -> None:
    fired: list[str] = []
    g = _recording_guard("g", fired)
    pipeline = build_pipeline([g])
    engine = load_policy_set_from_dict(
        {"roles": {"default": {"tools": {"send_email": {"mode": "allow"}}}}}
    )
    _run(pipeline, engine, "send_email")
    assert fired == ["g"]  # no stance for it -> runs as declared


def test_runner_fail_safe_when_stance_cannot_be_computed() -> None:
    """A refresh can swap in a policy whose stance can't be computed (a divergent-role
    PolicySet, not re-validated at refresh). The runner must not crash the call — it
    fails safe and runs every guard (R-GUARD-007)."""
    from types import SimpleNamespace

    from hexgate.guards.runner import _guard_stance

    divergent = load_policy_set_from_dict(
        {
            "roles": {
                "support": {"guards": {"g": {"enabled": False}}},
                "admin": {"guards": {"g": {"enabled": True}}},
            }
        }
    )
    with pytest.raises(PolicySetError):  # confirms effective_guards would raise
        divergent.effective_guards("some_tool")
    # ...but the runner swallows it and returns the empty stance (all guards run).
    assert _guard_stance(SimpleNamespace(policy=divergent), "some_tool") == {}


# --- Construction-time closed-world validation (fail-fast) ---------------------


def test_validate_guard_policy_rejects_undeclared_guard() -> None:
    from hexgate.guards.stance import validate_guard_policy

    g1 = _named_before("g1")
    engine = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"ghost_guard": {"enabled": False}}}}}
    )
    with pytest.raises(GuardClosedWorldError, match="ghost_guard"):
        validate_guard_policy(engine, [g1], agent_name="a")


def test_validate_guard_policy_noop_when_no_stance() -> None:
    from hexgate.guards.stance import validate_guard_policy

    g1 = _named_before("g1")
    engine = load_policy_set_from_dict(
        {"roles": {"default": {"tools": {"t": {"mode": "allow"}}}}}
    )
    assert validate_guard_policy(engine, [g1], agent_name="a") is None


def test_validate_guard_policy_fires_when_agent_declares_no_guards() -> None:
    """The 'guard deleted from code, policy still references it' case must not pass
    silently (R-GUARD-007)."""
    from hexgate.guards.stance import validate_guard_policy

    engine = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"ghost_guard": {"enabled": False}}}}}
    )
    with pytest.raises(GuardClosedWorldError, match="ghost_guard"):
        validate_guard_policy(engine, None, agent_name="a")


def test_validate_guard_policy_rejects_ambiguous_name() -> None:
    from hexgate.guards.stance import validate_guard_policy

    def _factory() -> object:
        def check(call: ToolCall) -> None:
            return None

        return before_tool(check)

    g_a, g_b = _factory(), _factory()  # both label 'check'
    engine = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"check": {"enabled": False}}}}}
    )
    with pytest.raises(GuardClosedWorldError, match="attached more than once"):
        validate_guard_policy(engine, [g_a, g_b], agent_name="a")


# --- PR4: authoring lints (R-GUARD-006 / R-GUARD-007) --------------------------


def _manifest(*names: str) -> SimpleNamespace:
    """A duck-typed manifest of guard names. lint_guards keys by name and counts
    duplicates (ambiguity); it does not read tool_names, so none is carried here."""
    return SimpleNamespace(
        guards=[SimpleNamespace(name=n, tool_names=None) for n in names]
    )


def _codes(lints: list) -> list[str]:
    return [lint.code for lint in lints]


def test_lint_unknown_guard_baseline() -> None:
    ps = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"ghost": {"enabled": False}}}}}
    )
    lints = lint_guards(ps, _manifest("secret_guard"), source="p.yaml")
    assert _codes(lints) == ["unknown-guard"]
    # error, not warning: the runtime stops cold, so the policy will crash the agent.
    assert lints[0].severity == "error"
    assert "ghost" in lints[0].message
    assert lints[0].source == "p.yaml"


def test_lint_declared_guard_is_clean() -> None:
    ps = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"secret_guard": {"enabled": False}}}}}
    )
    assert lint_guards(ps, _manifest("secret_guard"), source="p.yaml") == []


def test_lint_flags_unknown_guard_from_a_compose_policy() -> None:
    """lint_guards runs over the resolved PolicySet regardless of its source, so a guard
    authored in the compose `policy.yaml` entry file but not declared by the agent's
    manifest is flagged just like a classic one (R-GUARD-006)."""
    from hexgate.security.compose import resolve_text

    ps = resolve_text(
        "version: 1\n"
        "guards: { ghost_guard: { enabled: false } }\n"
        "tools: { a: { mode: allow } }\n"
    ).policy_set
    lints = lint_guards(ps, _manifest("secret_guard"), source="policy.yaml")
    assert _codes(lints) == ["unknown-guard"]
    assert "ghost_guard" in lints[0].message


def test_lint_ambiguous_guard_name() -> None:
    """A governed name attached more than once is flagged: the runtime stops cold on it
    (GuardClosedWorldError), so the lint must warn, not pass."""
    ps = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"w": {"enabled": False}}}}}
    )
    # Two manifest entries named 'w' -> ambiguous.
    lints = lint_guards(ps, _manifest("w", "w"), source="p.yaml")
    assert _codes(lints) == ["ambiguous-guard"]
    assert lints[0].severity == "error"


def test_lint_no_manifest_guards_flags_every_reference() -> None:
    ps = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"x": {"enabled": False}}}}}
    )
    assert _codes(lint_guards(ps, SimpleNamespace(guards=None), source="p.yaml")) == [
        "unknown-guard"
    ]
