"""Tests for the policy analyzer — soft lints over a linked bundle, plus the
cross-role `permissive-default` check over a resolved role map."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hexgate.security import (
    AgentPolicy,
    BaseToolPolicy,
    ModuleContent,
    PolicySet,
    analyze,
    analyze_policy,
    check,
    check_project,
    link_policy_set,
    load_policy_map,
    load_policy_set_from_dict,
)
from hexgate.security.analyzer import check_default_role_exposure
from hexgate.security.models import gate_args


def _mod(name, kind, tools, *, default_mode="allow"):
    return ModuleContent(
        name=name,
        kind=kind,
        policy=AgentPolicy(
            default_policy=BaseToolPolicy(mode=default_mode), tools=tools
        ),
        source=f"{name}.yaml",
        content_hash=f"hash-{name}",
    )


def _allow(constraints=None):
    return BaseToolPolicy(mode="allow", constraints=constraints or [])


def _deny(constraints=None):
    return BaseToolPolicy(mode="deny", constraints=constraints or [])


def _manifest(*tools, guards=(), skills=()):
    """Duck-typed AgentManifest: tools=[(name, [arg, ...]), ...] plus guard and
    skill names."""
    return SimpleNamespace(
        tools=[
            SimpleNamespace(
                name=name,
                input_schema=SimpleNamespace(properties={a: None for a in args}),
            )
            for name, args in tools
        ],
        guards=[SimpleNamespace(name=g) for g in guards],
        skills=[SimpleNamespace(name=s) for s in skills],
    )


def _codes(lints):
    return {(lint.code, lint.tool) for lint in lints}


# --- clean ---


def test_clean_bundle_has_no_lints():
    boundary = _mod("b", "boundary", {"refund": _allow(["args.amount <= 100"])})
    cap = _mod("c", "capability", {"refund": _allow()})
    manifest = _manifest(("refund", ["amount"]))
    assert check([boundary], [cap], manifest=manifest) == []


# --- dead-grant (provenance only, no manifest) ---


def test_dead_grant_when_ceiling_excludes_a_capability_grant():
    ceiling = _mod("org", "boundary", {"refund": _allow()}, default_mode="deny")
    cap = _mod("c", "capability", {"refund": _allow(), "send_email": _allow()})

    lints = check([ceiling], [cap])

    dead = [lint for lint in lints if lint.code == "dead-grant"]
    assert len(dead) == 1
    assert dead[0].tool == "send_email"
    assert dead[0].severity == "warning"
    assert dead[0].source == "c.yaml"
    assert "org" in dead[0].message  # names the shadowing boundary


def test_dead_grant_when_a_boundary_hard_denies_the_tool():
    # The most clear-cut dead grant: an unconditional boundary deny beats the
    # grant. This never enters trace.shadowed, so keying off the effective
    # policy (not shadowed) is what catches it.
    boundary = _mod("org", "boundary", {"wire": _deny()})  # floor, unconditional deny
    cap = _mod("c", "capability", {"wire": _allow()})

    lints = check([boundary], [cap])

    dead = [lint for lint in lints if lint.code == "dead-grant"]
    assert len(dead) == 1
    assert dead[0].tool == "wire"
    assert "denies" in dead[0].message


def test_dead_reach_grant_flagged_like_a_tool_grant():
    # Agent-level blocks compose through the linker (#124), so a reach grant a
    # ceiling never permits is a dead grant — the lint passes read effective_tools,
    # so the lowered agent.tool: key is covered like any tool.
    ceiling = ModuleContent(
        name="org",
        kind="boundary",
        policy=AgentPolicy(
            default_policy=BaseToolPolicy(mode="deny")
        ),  # lists no reach
        source="org.yaml",
        content_hash="hash-org",
    )
    cap = ModuleContent(
        name="c",
        kind="capability",
        policy=AgentPolicy(agents={"evil_bot": {"via": ["tool"], "mode": "allow"}}),
        source="c.yaml",
        content_hash="hash-c",
    )
    dead = [lint for lint in check([ceiling], [cap]) if lint.code == "dead-grant"]
    assert [lint.tool for lint in dead] == ["agent.tool:evil_bot"]


# --- redundant-grant ---


def test_redundant_grant_across_two_capabilities():
    c1 = _mod("c1", "capability", {"refund": _allow(["args.amount <= 100"])})
    c2 = _mod("c2", "capability", {"refund": _allow(["args.amount <= 100"])})

    lints = check([], [c1, c2])

    red = [lint for lint in lints if lint.code == "redundant-grant"]
    assert len(red) == 1
    assert red[0].tool == "refund"
    assert red[0].severity == "info"
    assert red[0].source == "c2.yaml"  # the later one is flagged


# --- link errors surface as an error lint, not an exception ---


def test_link_error_becomes_an_error_lint():
    bad = _mod("bad", "capability", {"refund": _deny()})
    lints = check([], [bad])
    assert len(lints) == 1
    assert lints[0].code == "link-error"
    assert lints[0].severity == "error"


# --- drift (needs a manifest) ---


def test_unknown_tool_severity_follows_failure_direction():
    # boundary ceiling naming a missing tool = fail-open (real tool uncapped) = error;
    # boundary deny on a missing tool = harmless = info;
    # capability drift = dead grant = warning.
    boundary = _mod(
        "b",
        "boundary",
        {"ghost_cap": _allow(), "ghost_deny": _deny()},
    )
    cap = _mod("c", "capability", {"ghost_tool": _allow()})
    manifest = _manifest(("refund", ["amount"]))  # none of these tools declared

    lints = check([boundary], [cap], manifest=manifest)
    by = {lint.tool: lint for lint in lints if lint.code == "unknown-tool"}

    assert by["ghost_cap"].severity == "error"  # boundary ceiling allow
    assert by["ghost_deny"].severity == "info"  # boundary deny, harmless
    assert by["ghost_tool"].severity == "warning"  # capability grant
    assert by["ghost_tool"].tier == "capability"


def test_drift_skipped_without_a_manifest():
    boundary = _mod("b", "boundary", {"delete_db": _deny()})
    cap = _mod("c", "capability", {"ghost_tool": _allow()})
    lints = check([boundary], [cap])  # no manifest
    assert not any(lint.code == "unknown-tool" for lint in lints)


def test_unknown_arg_flags_a_constraint_on_a_missing_parameter():
    boundary = _mod("b", "boundary", {"refund": _allow(['args.currency == "USD"'])})
    cap = _mod("c", "capability", {"refund": _allow()})
    manifest = _manifest(("refund", ["amount"]))  # accepts amount, not currency

    lints = check([boundary], [cap], manifest=manifest)

    arg = [lint for lint in lints if lint.code == "unknown-arg"]
    assert len(arg) == 1
    assert arg[0].tool == "refund"
    assert arg[0].source == "b.yaml"
    assert "currency" in arg[0].message


# --- analyze() over an existing result, and severity ordering ---


def test_analyze_sorts_errors_first():
    boundary = _mod("b", "boundary", {"ghost": _allow()})  # ceiling drift = error
    c1 = _mod("c1", "capability", {"refund": _allow(["args.amount <= 1"])})
    c2 = _mod("c2", "capability", {"refund": _allow(["args.amount <= 1"])})
    manifest = _manifest(("refund", ["amount"]))

    result = link_policy_set([boundary], [c1, c2])
    lints = analyze(result, [boundary], [c1, c2], manifest=manifest)

    from hexgate.security.analyzer import SEVERITY_RANK

    severities = [lint.severity for lint in lints]
    assert severities == sorted(severities, key=SEVERITY_RANK.get)
    assert ("unknown-tool", "ghost") in _codes(lints)  # error present
    assert ("redundant-grant", "refund") in _codes(lints)  # info present


def test_undefined_const_becomes_link_error_lint_not_traceback():
    # link_policy_set raises PolicySetError (undefined const) — check() must fold
    # it into a lint, not let it escape as a traceback.
    cap = _mod(
        "c", "capability", {"refund": _allow(["args.amount <= consts.max_refund"])}
    )
    lints = check([], [cap])
    assert [lint.code for lint in lints] == ["link-error"]
    assert lints[0].severity == "error"


def test_boundary_deny_arg_typo_is_error():
    # A boundary conditional deny with a typo'd arg inverts to an allow
    # (fail-open), so its arg drift must be an error, not a warning.
    boundary = _mod("org", "boundary", {"refund": _deny(["args.amoun > 1000"])})
    cap = _mod("c", "capability", {"refund": _allow()})
    manifest = _manifest(("refund", ["amount"]))

    lints = check([boundary], [cap], manifest=manifest)
    arg = [lint for lint in lints if lint.code == "unknown-arg"]
    assert len(arg) == 1
    assert arg[0].severity == "error"
    assert arg[0].tier == "boundary"


def test_constraint_erased_when_a_sibling_grant_is_unconditional():
    tight = _mod("tight", "capability", {"refund": _allow(["args.amount <= 100"])})
    loose = _mod("loose", "capability", {"refund": _allow()})  # unconditional
    lints = check([], [tight, loose])
    erased = [lint for lint in lints if lint.code == "constraint-erased"]
    assert len(erased) == 1
    assert erased[0].tool == "refund"
    assert erased[0].source == "tight.yaml"  # the constrained one is flagged
    assert "loose" in erased[0].message


def test_default_policy_constraints_rejected_as_link_error():
    from hexgate.security import AgentPolicy, BaseToolPolicy

    module = ModuleContent(
        name="b",
        kind="boundary",
        policy=AgentPolicy(
            default_policy=BaseToolPolicy(mode="allow", constraints=["args.x <= 1"])
        ),
        source="b.yaml",
        content_hash="h",
    )
    lints = check([module], [])
    assert [lint.code for lint in lints] == ["link-error"]
    assert "default_policy constraints" in lints[0].message


# ---------------------------------------------------------------------------
# permissive-default — cross-role exposure of the `default` fallback
# ---------------------------------------------------------------------------


def _policy_set(roles: dict[str, dict]) -> PolicySet:
    return load_policy_map(
        {name: AgentPolicy.model_validate(spec) for name, spec in roles.items()}
    )


def test_permissive_default_flags_a_grant_no_named_role_has() -> None:
    """A tool only `default` grants is reachable by any undefined role name."""
    lints = check_default_role_exposure(
        _policy_set(
            {
                "default": {"tools": {"delete_everything": {"mode": "allow"}}},
                "support": {"tools": {"read_file": {"mode": "allow"}}},
            }
        )
    )

    assert [lint.code for lint in lints] == ["permissive-default"]
    assert lints[0].severity == "warning"
    assert lints[0].tool == "delete_everything"


def test_permissive_default_flags_an_agent_grant_no_named_role_has() -> None:
    """An admission/agents grant on `default` is reachable by any undefined role
    name too, so it must lint via the lowered `agent.*` keys, not just `tools`."""
    lints = check_default_role_exposure(
        _policy_set(
            {
                "default": {"agents": {"admin-bot": {"mode": "allow"}}},
                "support": {"tools": {"read_file": {"mode": "allow"}}},
            }
        )
    )

    codes = [lint.code for lint in lints]
    assert codes == ["permissive-default", "permissive-default"]  # tool + handoff
    assert {lint.tool for lint in lints} == {
        "agent.tool:admin-bot",
        "agent.handoff:admin-bot",
    }


def test_permissive_default_is_quiet_when_a_named_role_also_grants_it() -> None:
    """A shared grant is intentional (typically a mixin), not exposure."""
    lints = check_default_role_exposure(
        _policy_set(
            {
                "default": {"tools": {"read_file": {"mode": "allow"}}},
                "support": {"tools": {"read_file": {"mode": "allow"}}},
            }
        )
    )

    assert lints == []


def test_permissive_default_is_quiet_for_a_least_privilege_default() -> None:
    lints = check_default_role_exposure(
        _policy_set(
            {
                "default": {"default_policy": {"mode": "deny"}},
                "support": {"tools": {"read_file": {"mode": "allow"}}},
            }
        )
    )

    assert lints == []


def test_permissive_default_is_quiet_for_a_single_role_policy() -> None:
    """A legacy flat policy.yaml *is* the `default` role — nothing to report."""
    lints = check_default_role_exposure(
        _policy_set({"default": {"tools": {"read_file": {"mode": "allow"}}}})
    )

    assert lints == []


def test_permissive_default_flags_a_granting_catch_all() -> None:
    """`default_policy: allow` under `default` exposes every unlisted tool."""
    lints = check_default_role_exposure(
        _policy_set(
            {
                "default": {"default_policy": {"mode": "allow"}},
                "support": {
                    "default_policy": {"mode": "deny"},
                    "tools": {"read_file": {"mode": "allow"}},
                },
            }
        )
    )

    assert [lint.code for lint in lints] == ["permissive-default"]
    assert "default_policy" in lints[0].message
    assert lints[0].tool is None


def test_permissive_default_flags_approval_required_grants_too() -> None:
    """`approval_required` still reaches the tool, just with a gate."""
    lints = check_default_role_exposure(
        _policy_set(
            {
                "default": {"tools": {"deploy": {"mode": "approval_required"}}},
                "support": {"tools": {"read_file": {"mode": "allow"}}},
            }
        )
    )

    assert len(lints) == 1
    assert lints[0].tool == "deploy"


def test_permissive_default_sees_through_inheritance() -> None:
    """The check runs on resolved policies, so inherited grants count."""
    lints = check_default_role_exposure(
        _policy_set(
            {
                "base": {"is_mixin": True, "tools": {"read_file": {"mode": "allow"}}},
                "default": {"tools": {"read_file": {"mode": "allow"}}},
                "support": {"inherits": ["base"]},
            }
        )
    )

    assert lints == []


# ---------------------------------------------------------------------------
# implicit-default — a roles document that never declares `default`
#
# The loader aliases the first concrete role as the fallback, so `default` and
# that role resolve to the SAME policy object. Every test above declares an
# explicit `default`, which is how a silent check shipped: comparing the
# fallback against a list that still contained itself matched every grant.
# ---------------------------------------------------------------------------


def test_implicit_default_is_flagged() -> None:
    """No `default` role means one named role silently became the fallback."""
    lints = check_default_role_exposure(
        _policy_set(
            {
                "billing": {"tools": {"refund": {"mode": "allow"}}},
                "support": {"tools": {"lookup": {"mode": "allow"}}},
            }
        )
    )

    codes = [lint.code for lint in lints]
    assert "implicit-default" in codes
    assert all(lint.severity == "warning" for lint in lints)
    assert "billing" in next(
        lint.message for lint in lints if lint.code == "implicit-default"
    )


def test_implicit_default_still_reports_the_grants_it_exposes() -> None:
    """The aliased role's own grants ARE the exposure — they must be listed."""
    lints = check_default_role_exposure(
        _policy_set(
            {
                "billing": {"tools": {"refund": {"mode": "allow"}}},
                "support": {"tools": {"lookup": {"mode": "allow"}}},
            }
        )
    )

    assert "refund" in {lint.tool for lint in lints}


def test_explicit_default_argument_is_not_an_implicit_default() -> None:
    """`load_policy_map(default=...)` is a deliberate choice, not an accident."""
    lints = load_policy_map(
        {
            "billing": AgentPolicy.model_validate(
                {"tools": {"refund": {"mode": "allow"}}}
            ),
            "support": AgentPolicy.model_validate(
                {"tools": {"lookup": {"mode": "allow"}}}
            ),
        },
        default="billing",
    )

    assert [lint.code for lint in check_default_role_exposure(lints)] != [
        "implicit-default"
    ]


# --- check_project: per-role attribution + project-level lints -------------


def test_check_project_tags_a_dead_grant_with_its_role():
    ceiling = _mod("org", "boundary", {"refund": _allow()}, default_mode="deny")
    pay = _mod("pay", "capability", {"refund": _allow(), "send_email": _allow()})

    lints = check_project([ceiling], [pay], {"support": ["pay"]})

    dead = [lint for lint in lints if lint.code == "dead-grant"]
    assert len(dead) == 1
    assert dead[0].tool == "send_email"  # ceiling excludes it
    assert dead[0].role == "support"


def test_unused_capability_is_flagged():
    used = _mod("used", "capability", {"x": _allow()})
    unused = _mod("unused", "capability", {"y": _allow()})

    lints = check_project([], [used, unused], {"default": ["used"]})

    un = [lint for lint in lints if lint.code == "unused-capability"]
    assert len(un) == 1
    assert un[0].source == "unused.yaml"
    assert "unused" in un[0].message


def test_no_default_role_is_flagged():
    cap = _mod("c", "capability", {"x": _allow()})
    lints = check_project([], [cap], {"billing": ["c"]})
    nd = [lint for lint in lints if lint.code == "no-default-role"]
    assert len(nd) == 1
    # role stays None so a role-scoped `check --role X` still surfaces it.
    assert nd[0].role is None


def test_check_project_unknown_capability_is_a_link_error():
    # Same contract as resolve_for_project: an unknown capability name in a role
    # fails, it is not silently dropped.
    cap = _mod("c", "capability", {"x": _allow()})
    lints = check_project([], [cap], {"r": ["missing"]})
    assert [lint.code for lint in lints] == ["link-error"]


def test_check_project_surfaces_named_agent_column_link_error():
    # A named-agent column importing an unknown capability must show as a
    # link-error lint (the "*" column alone would never touch it).
    from hexgate.security import AgentBinding

    read_only = _mod("read_only", "capability", {"view": _allow()})
    roles = {
        "member": {
            "*": AgentBinding(capabilities=("read_only",)),
            "billing_bot": AgentBinding(capabilities=("nonexistent",)),
        }
    }
    lints = check_project([], [read_only], roles)
    assert any(
        lint.code == "link-error" and "billing_bot" in lint.message for lint in lints
    )


def test_no_roles_emit_no_project_lints():
    # None (no roles.yaml) -> one default importing everything. Nothing unused,
    # and the default is present, so neither project-level lint fires.
    cap = _mod("c", "capability", {"x": _allow()})
    lints = check_project([], [cap], None)
    codes = {lint.code for lint in lints}
    assert "unused-capability" not in codes
    assert "no-default-role" not in codes


def test_implicit_default_grant_messages_name_the_aliased_role() -> None:
    """The aliased role IS a named role that grants the tool, so the message must
    not claim otherwise — it names the fallback instead."""
    lints = check_default_role_exposure(
        _policy_set(
            {
                "billing": {"tools": {"refund": {"mode": "allow"}}},
                "support": {"tools": {"lookup": {"mode": "allow"}}},
            }
        )
    )

    grant = next(lint for lint in lints if lint.tool == "refund")
    assert "billing" in grant.message
    assert "no named role" not in grant.message


def test_authored_default_grant_messages_keep_the_no_named_role_wording() -> None:
    lints = check_default_role_exposure(
        _policy_set(
            {
                "default": {"tools": {"deploy": {"mode": "allow"}}},
                "support": {"tools": {"lookup": {"mode": "allow"}}},
            }
        )
    )

    grant = next(lint for lint in lints if lint.tool == "deploy")
    assert "no named role does" in grant.message


def test_run_constraints_are_not_linted_as_unknown_args():
    """``_unknown_args`` only inspects ``args``-rooted paths, so ``run.*``
    passes through untouched."""
    boundary = _mod("b", "boundary", {"refund": _allow(["run.elapsed_seconds < 300"])})
    cap = _mod("c", "capability", {"refund": _allow()})
    manifest = _manifest(("refund", ["amount"]))

    lints = check([boundary], [cap], manifest=manifest)

    assert not [lint for lint in lints if lint.code == "unknown-arg"]


# --- analyze_policy: every check over a resolved policy set ---


def test_analyze_policy_happy_path():
    ps = load_policy_set_from_dict(
        {
            "guards": {"secret_redactor": {"enabled": False}},
            "tools": {"refund": {"mode": "allow", "constraints": ["args.amount < 5"]}},
        }
    )
    manifest = _manifest(("refund", ["amount"]), guards=["secret_redactor"])
    assert analyze_policy(ps, manifest=manifest) == []


def test_when_no_manifest_then_manifest_checks_are_skipped():
    ps = load_policy_set_from_dict(
        {
            "guards": {"secret_redacter": {"enabled": False}},
            "tools": {"refund": {"mode": "allow", "constraints": ["args.amont < 5"]}},
        }
    )
    assert analyze_policy(ps) == []
    manifest = _manifest(("refund", ["amount"]), guards=["secret_redactor"])
    assert {lint.code for lint in analyze_policy(ps, manifest=manifest)} == {
        "unknown-guard",
        "unknown-arg",
    }


def test_when_roles_disagree_on_guards_then_guard_divergence():
    ps = load_policy_set_from_dict(
        {
            "roles": {
                "default": {"guards": {"g": {"enabled": False}}},
                "admin": {"guards": {"g": {"enabled": True}}},
            }
        }
    )
    lints = analyze_policy(ps, source="policy.yaml")
    assert [(lint.code, lint.severity, lint.source) for lint in lints] == [
        ("guard-divergence", "error", "policy.yaml")
    ]


def test_when_an_arg_is_unknown_then_severity_follows_its_polarity():
    ps = load_policy_set_from_dict(
        {
            "tools": {
                "refund": {"mode": "allow", "constraints": ["args.amont < 5"]},
                "wipe": {"mode": "allow", "constraints": ["not (args.forse == true)"]},
            }
        }
    )
    manifest = _manifest(("refund", ["amount"]), ("wipe", ["force"]))
    lints = analyze_policy(ps, manifest=manifest)
    # A missing arg compares False: a grant fails closed, its negation fails open.
    assert [(lint.code, lint.tool, lint.severity) for lint in lints] == [
        ("unknown-arg", "wipe", "error"),
        ("unknown-arg", "refund", "warning"),
    ]
    assert all(lint.role == "default" for lint in lints)


def test_when_a_deny_constrains_an_unknown_arg_then_no_lint():
    # A resolved deny is unconditional: its constraints are never evaluated.
    ps = load_policy_set_from_dict(
        {"tools": {"wipe": {"mode": "deny", "constraints": ["args.forse == true"]}}}
    )
    assert analyze_policy(ps, manifest=_manifest(("wipe", ["force"]))) == []


def test_when_a_role_aliases_default_then_its_drift_names_that_role_once():
    ps = load_policy_set_from_dict(
        {"roles": {"admin": {"tools": {"refnd": {"mode": "allow"}}}}}
    )
    lints = analyze_policy(ps, manifest=_manifest(("refund", [])))
    assert [lint.role for lint in lints if lint.code == "unknown-tool"] == ["admin"]


def test_when_default_is_named_explicitly_then_its_drift_names_that_role_once():
    # An explicit default= leaves aliased_default unset; default is still the
    # very same policy object as admin.
    admin = AgentPolicy(tools={"refnd": BaseToolPolicy(mode="allow")})
    ps = load_policy_map({"admin": admin, "viewer": AgentPolicy()}, default="admin")
    lints = analyze_policy(ps, manifest=_manifest(("refund", [])))
    assert [lint.role for lint in lints if lint.code == "unknown-tool"] == ["admin"]


def test_when_a_source_is_given_then_every_lint_carries_it():
    ps = load_policy_set_from_dict(
        {
            "roles": {
                "default": {"tools": {"refund": {"mode": "allow"}}},
                "admin": {"tools": {}},
            }
        }
    )
    manifest = _manifest(("refund", []), guards=[])
    guards = {"secret_redactor": {"enabled": False}}
    ps_with_drift = load_policy_set_from_dict(
        {
            "constraints": ["user.x == 1"],
            "roles": {
                "default": {"guards": guards, "tools": {"refund": {"mode": "allow"}}},
                "admin": {"guards": guards, "tools": {"refnd": {"mode": "allow"}}},
            },
        }
    )
    lints = analyze_policy(ps, source="policy.yaml")
    lints += analyze_policy(ps_with_drift, manifest=manifest, source="policy.yaml")
    assert {"permissive-default", "unknown-guard", "unknown-tool", "unknown-root"} <= {
        lint.code for lint in lints
    }
    assert {lint.source for lint in lints} == {"policy.yaml"}


def test_when_a_tool_is_unknown_then_severity_follows_the_rule_mode():
    ps = load_policy_set_from_dict(
        {"tools": {"refnd": {"mode": "allow"}, "wipe_db": {"mode": "deny"}}}
    )
    lints = analyze_policy(ps, manifest=_manifest(("refund", [])))
    assert {(lint.code, lint.tool, lint.severity) for lint in lints} == {
        ("unknown-tool", "refnd", "warning"),
        ("unknown-tool", "wipe_db", "info"),
    }


def test_when_a_key_is_not_a_manifest_tool_then_no_unknown_tool():
    ps = load_policy_set_from_dict(
        {
            "tools": {
                "net.http_request": {"mode": "allow"},
                "net.tcp_connect": {"mode": "allow"},
                "agent.run": {"mode": "allow"},
                "agent.tool:other": {"mode": "allow"},
                "skill:triage": {"mode": "allow"},
                "skill.resource:triage": {"mode": "allow"},
                "skill.script:triage": {"mode": "allow"},
            },
            "_resolved": True,
        }
    )
    assert analyze_policy(ps, manifest=_manifest(skills=["triage"])) == []


def test_when_the_default_allows_then_a_stricter_rule_on_a_typo_is_an_error():
    # The real tool falls through to the allow default: it runs looser than meant.
    ps = load_policy_set_from_dict(
        {
            "default_policy": {"mode": "allow"},
            "tools": {
                "delete_databse": {"mode": "deny"},
                "refnd": {"mode": "approval_required"},
                "refnd_capped": {"mode": "allow", "constraints": ["args.amount < 5"]},
                "lookp": {"mode": "allow"},
            },
        }
    )
    lints = analyze_policy(ps, manifest=_manifest(("refund", ["amount"])))
    assert {(lint.tool, lint.severity) for lint in lints} == {
        ("delete_databse", "error"),
        ("refnd", "error"),
        ("refnd_capped", "error"),
        ("lookp", "warning"),
    }


def test_when_an_unknown_arg_is_used_twice_then_the_worst_severity_wins():
    ps = load_policy_set_from_dict(
        {
            "tools": {
                "wipe": {
                    "mode": "allow",
                    "constraints": ["args.forse == false", "not (args.forse == true)"],
                }
            }
        }
    )
    lints = analyze_policy(ps, manifest=_manifest(("wipe", ["force"])))
    assert [(lint.code, lint.severity) for lint in lints] == [("unknown-arg", "error")]


def test_when_a_module_names_an_egress_tool_then_no_unknown_tool():
    boundary = _mod("b", "boundary", {"net.http_request": _allow()})
    cap = _mod("c", "capability", {"net.http_request": _allow()})
    lints = check([boundary], [cap], manifest=_manifest())
    assert not [lint for lint in lints if lint.code == "unknown-tool"]


_RESTRICTIONS = {
    "none": {},
    "constraints": {"constraints": ["args.n < 5"]},
    "file_scope": {"file_scope": {"allowed_paths": ["/data/*"]}},
}
_STRICTNESS = {"allow": 0, "needs_approval": 1, "deny": 2}


@pytest.mark.parametrize("restriction", sorted(_RESTRICTIONS))
@pytest.mark.parametrize("default_mode", ["allow", "approval_required", "deny"])
@pytest.mark.parametrize("rule_mode", ["allow", "approval_required", "deny"])
def test_when_a_tool_is_misspelled_then_error_iff_the_real_tool_runs_looser(
    rule_mode, default_mode, restriction
):
    """Grades against the real evaluator: the misspelled rule is an error exactly
    when some call to the real tool now gets a looser verdict than intended."""
    rule = {"mode": rule_mode, **_RESTRICTIONS[restriction]}

    def policy(tool):
        doc = {"default_policy": {"mode": default_mode}, "tools": {tool: rule}}
        return load_policy_set_from_dict(doc)

    intended, typo = policy("read_file"), policy("read_fiel")
    calls = [{"path": p, "n": n} for p in ("/data/x", "/etc/passwd") for n in (1, 99)]

    def strictness(ps, args):
        verdict = ps.evaluate(role=None, tool="read_file", args=args)
        return _STRICTNESS[verdict.outcome.value]

    looser = any(strictness(typo, c) < strictness(intended, c) for c in calls)
    manifest = _manifest(("read_file", ["path", "n"]))
    [lint] = [
        lint
        for lint in analyze_policy(typo, manifest=manifest)
        if lint.code == "unknown-tool"
    ]
    assert (lint.severity == "error") == looser


def test_when_an_egress_rule_names_an_unknown_arg_then_unknown_arg():
    ps = load_policy_set_from_dict(
        {
            "tools": {
                "net.http_request": {
                    "mode": "allow",
                    "constraints": [
                        'not (args.hots == "evil.com")',
                        "args.port == 443",
                    ],
                },
                "net.tcp_connect": {
                    "mode": "allow",
                    "constraints": ["args.prot == 5432"],
                },
            }
        }
    )
    lints = analyze_policy(ps, manifest=_manifest())
    assert {(lint.code, lint.tool, lint.severity) for lint in lints} == {
        ("unknown-arg", "net.http_request", "error"),
        ("unknown-arg", "net.tcp_connect", "warning"),
    }


def test_when_a_module_egress_rule_names_an_unknown_arg_then_unknown_arg():
    boundary = _mod(
        "b", "boundary", {"net.http_request": _allow(['args.hots == "api.x.com"'])}
    )
    cap = _mod("c", "capability", {"net.http_request": _allow()})
    lints = check([boundary], [cap], manifest=_manifest())
    assert [
        (lint.code, lint.tool) for lint in lints if lint.code.startswith("unknown")
    ] == [("unknown-arg", "net.http_request")]


def test_egress_tool_args_match_what_the_proxy_builds():
    from hexgate.egress.model import connect_to_args, http_to_args
    from hexgate.egress.tcp import tcp_to_args
    from hexgate.security.network import (
        EGRESS_TOOL_ARGS,
        NET_HTTP_REQUEST,
        NET_TCP_CONNECT,
    )

    built = set(connect_to_args("h", 443)) | set(http_to_args("GET", "http://h/p?q"))
    assert built == EGRESS_TOOL_ARGS[NET_HTTP_REQUEST]
    assert set(tcp_to_args("h", 5432)) == EGRESS_TOOL_ARGS[NET_TCP_CONNECT]


def test_when_a_module_arg_typo_sits_under_not_then_severity_flips():
    # A capability's negated typo is always True (fail-open); a boundary deny is
    # itself wrapped in ``not``, so its negated typo cancels out (fail-closed).
    cap = _mod("c", "capability", {"refund": _allow(["not (args.amoun > 1000)"])})
    boundary = _mod(
        "b",
        "boundary",
        {"refund": BaseToolPolicy(mode="deny", constraints=["not (args.amoun > 5)"])},
    )
    lints = check([boundary], [cap], manifest=_manifest(("refund", ["amount"])))
    assert {
        (lint.tier, lint.severity) for lint in lints if lint.code == "unknown-arg"
    } == {("capability", "error"), ("boundary", "warning")}


def _unknown_args_of(doc, *tools):
    lints = analyze_policy(load_policy_set_from_dict(doc), manifest=_manifest(*tools))
    return [
        (lint.severity, lint.message, lint.role)
        for lint in lints
        if lint.code == "unknown-arg"
    ]


def test_when_a_policy_level_constraint_names_an_unknown_arg_then_once_per_policy():
    # File-level constraints are copied into every role; a negated typo is
    # always True, so the run-wide fence never fires.
    doc = {
        "constraints": ["not (args.amout > 1000)"],
        "roles": {
            "default": {"tools": {"refund": {"mode": "allow"}}},
            "admin": {"tools": {"refund": {"mode": "allow"}}},
        },
    }
    assert _unknown_args_of(doc, ("refund", ["amount"])) == [
        (
            "error",
            "a policy-level constraint uses args.amout, which no tool it applies "
            "to accepts",
            None,
        )
    ]


def test_when_roles_misuse_one_arg_differently_then_the_worst_severity_wins():
    doc = {
        "roles": {
            # Roles are visited sorted: the error sits between two warnings, so
            # neither first-wins nor last-wins passes.
            "admin": {"constraints": ["args.amout < 5"]},
            "default": {"constraints": ["not (args.amout > 5)"]},
            "zeta": {"constraints": ["args.amout < 7"]},
        },
    }
    assert [s for s, _, _ in _unknown_args_of(doc, ("refund", ["amount"]))] == ["error"]


def test_when_one_role_carries_a_constraint_typo_then_the_lint_names_it():
    doc = {
        "roles": {
            "default": {"tools": {"refund": {"mode": "allow"}}},
            "support": {"constraints": ["args.amout < 5"]},
        },
    }
    assert _unknown_args_of(doc, ("refund", ["amount"])) == [
        (
            "warning",
            "a policy-level constraint in role 'support' uses args.amout, which "
            "no tool it applies to accepts",
            "support",
        )
    ]


def test_when_a_policy_level_arg_exists_on_some_tool_then_no_unknown_arg():
    # A fence on an arg only some tools take is deliberate, not a typo.
    doc = {
        "constraints": ["args.amount < 100"],
        "tools": {"refund": {"mode": "allow"}, "lookup": {"mode": "allow"}},
    }
    tools = (("refund", ["amount"]), ("lookup", ["order_id"]))
    assert _unknown_args_of(doc, *tools) == []


def test_when_a_default_constraint_names_an_unknown_arg_then_unknown_arg():
    doc = {
        "default_policy": {"mode": "allow", "constraints": ["args.amout < 5"]},
        "tools": {},
    }
    assert [s for s, _, _ in _unknown_args_of(doc, ("refund", ["amount"]))] == [
        "warning"
    ]


def test_when_a_default_never_applies_then_its_constraints_are_not_checked():
    tools = ("refund", ["amount"])
    deny_default = {
        "default_policy": {"mode": "deny", "constraints": ["args.amout < 5"]},
    }
    nothing_falls_through = {
        "default_policy": {"mode": "allow", "constraints": ["args.amout < 5"]},
        # Egress tools fall through to the default too, so list them.
        "tools": {
            "refund": {"mode": "allow"},
            "net.http_request": {"mode": "deny"},
            "net.tcp_connect": {"mode": "deny"},
        },
    }
    assert _unknown_args_of(deny_default, tools) == []
    assert _unknown_args_of(nothing_falls_through, tools) == []


# --- gate args: what a constraint on an agent or skill key may read ---


class _RecordingEnforcer:
    """Enough of a PolicyEnforcer for a gate: records each decision it asks for."""

    agent_name = "orchestrator"
    policy = SimpleNamespace(
        declares_admission=lambda: True, declares_reach=lambda: True
    )

    def __init__(self):
        self.decided = {}

    def decide(self, key, args):
        self.decided[key] = args
        return SimpleNamespace(allowed=True)


def test_admission_and_reach_args_match_what_the_agent_gates_build():
    from hexgate.security.agent_gate import AgentGate, ReachGate

    enforcer = _RecordingEnforcer()
    AgentGate(enforcer).check_admission()
    ReachGate(enforcer).check_reach("billing", via="handoff")
    ReachGate(enforcer).check_reach("billing", via="tool")
    assert {key: set(args) for key, args in enforcer.decided.items()} == {
        key: gate_args(key)
        for key in ("agent.run", "agent.handoff:billing", "agent.tool:billing")
    }


@pytest.mark.parametrize("via", ["instructions", "resource"])
def test_skill_args_match_what_the_langchain_skill_seam_builds(via):
    from hexgate.adapters.langchain.skills import _SkillRead
    from hexgate.manifest.langchain import SkillLocation

    location = SkillLocation("pdf", "/skills/pdf/SKILL.md", backend=None)
    override = _SkillRead(via, location, "/skills/pdf/SKILL.md").override(None)
    assert set(override.args) == gate_args(override.key)


@pytest.mark.parametrize("via", ["instructions", "resource", "script"])
def test_skill_args_match_what_the_google_skill_seam_builds(via):
    from hexgate.adapters.google.tools import _skill_decision

    call = {"skill_name": "pdf", "file_path": "x.py", "args": {}}
    key, args = _skill_decision(object(), via, call)
    assert set(args) == gate_args(key)


@pytest.mark.parametrize(
    ("block", "key"),
    [
        (
            {"admission": {"mode": "allow", "constraints": ['args.agnt == "a"']}},
            "agent.run",
        ),
        (
            {
                "agents": {
                    "b": {
                        "mode": "allow",
                        "via": ["tool"],
                        "constraints": ['args.trgt == "b"'],
                    }
                }
            },
            "agent.tool:b",
        ),
        (
            {
                "skills": {
                    "pdf": {
                        "mode": "allow",
                        "via": ["resource"],
                        "constraints": ['args.file_pth == "x"'],
                    }
                }
            },
            "skill.resource:pdf",
        ),
        (
            # A script argument doesn't reach a skill's instructions.
            {
                "skills": {
                    "pdf": {
                        "mode": "allow",
                        "via": ["instructions"],
                        "constraints": ["args.script_args == []"],
                    }
                }
            },
            "skill:pdf",
        ),
    ],
)
def test_when_a_gate_rule_reads_an_arg_its_gate_never_passes_then_unknown_arg(
    block, key
):
    ps = load_policy_set_from_dict(block)
    lints = analyze_policy(ps, manifest=_manifest(skills=["pdf"]))
    assert [(lint.code, lint.tool, lint.severity) for lint in lints] == [
        ("unknown-arg", key, "warning")
    ]


def test_when_a_gate_rule_reads_its_gate_args_then_no_unknown_arg():
    ps = load_policy_set_from_dict(
        {
            "admission": {"mode": "allow", "constraints": ['args.agent == "a"']},
            "agents": {"b": {"mode": "allow", "constraints": ['args.via == "tool"']}},
            "skills": {
                "pdf": {
                    "mode": "allow",
                    "via": ["script"],
                    "constraints": [
                        'args.skill == "pdf"',
                        "count(args.script_args) < 3",
                    ],
                }
            },
        }
    )
    assert analyze_policy(ps, manifest=_manifest(skills=["pdf"])) == []


def test_when_a_policy_level_constraint_reads_a_gate_arg_then_no_unknown_arg():
    doc = {
        "constraints": [
            'args.target != "x"',
            'args.skill != "shell"',
            "count(args.script_args) < 3",
        ],
        "agents": {"billing": {"mode": "allow"}},
        "skills": {"pdf": {"mode": "allow", "via": ["script"]}},
    }
    assert _unknown_args_of(doc, ("refund", ["amount"])) == []


@pytest.mark.parametrize(
    "gates",
    [
        {},
        # Listed, but denied: a deny never runs the policy-level constraints.
        {"skills": {"pdf": {"mode": "deny"}}},
    ],
)
def test_when_no_gate_grant_passes_an_arg_then_a_policy_level_use_is_unknown(gates):
    # Always True under ``not``: the fence on read_file's path never fires.
    doc = {
        "constraints": ['not startswith(args.file_path, "/etc")'],
        "tools": {"read_file": {"mode": "allow"}},
        **gates,
    }
    assert [s for s, _, _ in _unknown_args_of(doc, ("read_file", ["path"]))] == [
        "error"
    ]


def test_when_a_key_is_not_reserved_then_gate_args_raises():
    with pytest.raises(ValueError, match="'refund' is not a reserved key"):
        gate_args("refund")


def test_when_a_module_gate_rule_reads_an_unknown_arg_then_unknown_arg():
    cap = ModuleContent(
        name="c",
        kind="capability",
        policy=AgentPolicy(
            admission=BaseToolPolicy(mode="allow", constraints=['args.agnt == "a"'])
        ),
        source="c.yaml",
        content_hash="hash-c",
    )
    lints = check([], [cap], manifest=_manifest())
    assert _codes(lints) == {("unknown-arg", "agent.run")}


# --- unknown-skill ---


def test_when_a_skill_is_not_in_the_manifest_then_unknown_skill_by_rule_mode():
    ps = load_policy_set_from_dict(
        {
            "skills": {
                "shell": {"mode": "deny"},
                "zip": {"mode": "allow"},
                "pdff": {"mode": "allow"},
                "pdf": {"mode": "allow"},
            }
        }
    )
    lints = analyze_policy(ps, manifest=_manifest(skills=["pdf"]))
    # Once per skill, though each lowers to a key per level; sorted by name
    # within a severity.
    assert [(lint.severity, lint.message, lint.role) for lint in lints] == [
        (
            "warning",
            "role 'default' governs skill 'pdff', which the agent's manifest "
            "doesn't declare",
            "default",
        ),
        (
            "warning",
            "role 'default' governs skill 'zip', which the agent's manifest "
            "doesn't declare",
            "default",
        ),
        (
            "info",
            "role 'default' governs skill 'shell', which the agent's manifest "
            "doesn't declare",
            "default",
        ),
    ]


def test_when_a_skill_name_is_padded_then_it_matches_the_manifest_skill():
    ps = load_policy_set_from_dict({"skills": {" pdf ": {"mode": "allow"}}})
    assert analyze_policy(ps, manifest=_manifest(skills=["pdf "])) == []


def test_when_the_manifest_lists_no_skills_then_every_skill_is_unknown():
    ps = load_policy_set_from_dict({"skills": {"pdf": {"mode": "allow"}}})
    assert {lint.code for lint in analyze_policy(ps, manifest=_manifest())} == {
        "unknown-skill"
    }


def test_when_the_manifest_skills_are_missing_then_no_unknown_skill():
    # The builders record None for no skills and for a listing that failed,
    # while the skill gate still runs, so None doesn't mean "no skills".
    manifest = _manifest()
    manifest.skills = None
    ps = load_policy_set_from_dict({"skills": {"pdf": {"mode": "allow"}}})
    assert analyze_policy(ps, manifest=manifest) == []

    policy = AgentPolicy(
        default_policy=BaseToolPolicy(mode="allow"), skills={"pdf": {"mode": "allow"}}
    )
    boundary = ModuleContent("b", "boundary", policy, "b.yaml", "hash-b")
    assert check([boundary], [], manifest=manifest) == []


def test_when_a_module_governs_an_unknown_skill_then_unknown_skill():
    cap = ModuleContent(
        name="c",
        kind="capability",
        policy=AgentPolicy(skills={"pdff": {"mode": "allow"}}),
        source="c.yaml",
        content_hash="hash-c",
    )
    lints = check([], [cap], manifest=_manifest(skills=["pdf"]))
    assert [(lint.code, lint.source, lint.tier) for lint in lints] == [
        ("unknown-skill", "c.yaml", "capability")
    ]


# --- unknown-root: a constraint path no call sets ---


def _unknown_roots_of(doc):
    lints = analyze_policy(load_policy_set_from_dict(doc))
    return [
        (lint.severity, lint.message) for lint in lints if lint.code == "unknown-root"
    ]


@pytest.mark.parametrize(
    ("constraint", "path"),
    [
        ('user.department == "finance"', "user.department"),
        ('role.name == "admin"', "role.name"),
        ('startswith(tool.name, "net")', "tool.name"),
        ("count(caller.groups) > 0", "caller.groups"),
    ],
)
def test_when_a_constraint_path_has_no_root_then_unknown_root(constraint, path):
    doc = {"tools": {"refund": {"mode": "allow", "constraints": [constraint]}}}
    assert _unknown_roots_of(doc) == [
        (
            "warning",
            f"a constraint reads {path}: no call sets it. A path starts with "
            "args., ctx. or run., and role and tool are plain strings",
        )
    ]


def test_when_constraint_paths_use_a_root_then_no_unknown_root():
    doc = {
        "constraints": ["run.tool_calls < 50"],
        "tools": {
            "refund": {
                "mode": "allow",
                "constraints": [
                    "args.amount < 5",
                    'ctx.department == "finance"',
                    'role == "admin"',
                    'tool != "x"',
                    "args.amount <= consts.cap",
                ],
            }
        },
        "consts": {"cap": 5},
    }
    assert _unknown_roots_of(doc) == []


def test_when_an_unknown_root_sits_under_not_then_it_is_an_error():
    # Always True: the fence never fires.
    doc = {"constraints": ['not (user.department == "finance")']}
    assert [s for s, _ in _unknown_roots_of(doc)] == ["error"]


def test_when_roles_share_an_unknown_root_then_once_at_the_worst_severity():
    doc = {
        "roles": {
            "admin": {"constraints": ["user.x == 1"]},
            "default": {"constraints": ["not (user.x == 1)"]},
            "zeta": {
                "default_policy": {"mode": "allow", "constraints": ["user.x == 2"]}
            },
        }
    }
    assert [s for s, _ in _unknown_roots_of(doc)] == ["error"]


def test_when_a_deny_constrains_an_unknown_root_then_no_unknown_root():
    doc = {
        "default_policy": {"mode": "deny", "constraints": ["user.x == 1"]},
        "tools": {"refund": {"mode": "deny", "constraints": ["user.x == 1"]}},
    }
    assert _unknown_roots_of(doc) == []


def test_when_a_gate_rule_has_an_unknown_root_then_unknown_root():
    doc = {"admission": {"mode": "allow", "constraints": ['caller.team == "ops"']}}
    assert [s for s, _ in _unknown_roots_of(doc)] == ["warning"]


def test_when_a_module_boundary_deny_has_an_unknown_root_then_it_is_an_error():
    # The linker folds a boundary deny into ``not (...)``: always True, fail-open.
    boundary = _mod("b", "boundary", {"refund": _deny(["user.x == 1"])})
    cap = _mod("c", "capability", {"refund": _allow(["caller.y == 1"])})
    lints = check([boundary], [cap])
    assert [(lint.code, lint.severity, lint.source, lint.tier) for lint in lints] == [
        ("unknown-root", "error", "b.yaml", "boundary"),
        ("unknown-root", "warning", "c.yaml", "capability"),
    ]


def test_path_roots_match_what_check_constraints_sets():
    from hexgate.security.constraints import PATH_ROOTS, check_constraints

    for root in PATH_ROOTS:
        # Raises if <root>.x is missing from the evaluation context.
        check_constraints(
            [f"{root}.x == 1"], {"x": 1}, "t", attributes={"x": 1}, run={"x": 1}
        )


def test_when_a_default_constraint_has_an_unknown_root_then_unknown_root():
    doc = {"default_policy": {"mode": "allow", "constraints": ["user.x == 1"]}}
    assert [s for s, _ in _unknown_roots_of(doc)] == ["warning"]


def test_when_several_paths_have_no_root_then_they_are_sorted():
    doc = {"constraints": ["user.z == 1", "caller.a == 1"]}
    assert [m.split()[3] for _, m in _unknown_roots_of(doc)] == [
        "caller.a:",
        "user.z:",
    ]


def test_when_a_module_boundary_grants_an_unknown_skill_then_it_is_an_error():
    # The boundary's fence never reaches the real skill, which runs on the
    # capability's grant alone.
    def skills_module(name, kind, skills):
        policy = AgentPolicy(default_policy=BaseToolPolicy(mode="allow"), skills=skills)
        return ModuleContent(name, kind, policy, f"{name}.yaml", f"hash-{name}")

    fence = {"mode": "allow", "constraints": ['args.file_path != "x"']}
    boundary = skills_module("b", "boundary", {"pdf-tolls": fence})
    cap = skills_module("c", "capability", {"pdf-tools": {"mode": "allow"}})
    lints = check([boundary], [cap], manifest=_manifest(skills=["pdf-tools"]))
    assert [(lint.code, lint.severity, lint.tier) for lint in lints] == [
        ("unknown-skill", "error", "boundary")
    ]


@pytest.mark.parametrize(
    ("constraint", "unset"),
    [
        ("count(args) < 3", []),
        ("count(ctx) > 0", []),
        ("every(args, . != null)", []),
        ("count(groups) > 0", ["groups"]),
        ("any(role, . == 1)", []),
    ],
)
def test_when_a_collection_is_a_lone_identifier_then_only_a_root_or_fact_is_set(
    constraint, unset
):
    doc = {"tools": {"refund": {"mode": "allow", "constraints": [constraint]}}}
    ps = load_policy_set_from_dict(doc)
    lints = analyze_policy(ps, manifest=_manifest(("refund", ["amount"])))
    assert [lint.message.split()[3].rstrip(":") for lint in lints] == unset


def test_when_the_manifest_skills_hit_the_cap_then_no_unknown_skill():
    # The list was cut at MAX_SKILLS, so a skill past the cap is still real.
    from hexgate.manifest.models import MAX_SKILLS

    names = [f"s{i}" for i in range(MAX_SKILLS)]
    ps = load_policy_set_from_dict({"skills": {"s_past_cap": {"mode": "allow"}}})
    assert analyze_policy(ps, manifest=_manifest(skills=names)) == []
    under_cap = _manifest(skills=names[:-1])
    assert {lint.code for lint in analyze_policy(ps, manifest=under_cap)} == {
        "unknown-skill"
    }


def test_when_roles_share_a_module_with_an_unknown_root_then_it_is_reported_once():
    boundary = _mod("b", "boundary", {"refund": _deny(["user.x == 1"])})
    cap = _mod("c", "capability", {"refund": _allow(["caller.y == 1"])})
    roles = {"default": ["c"], "admin": ["c"], "member": ["c"]}
    lints = check_project([boundary], [cap], roles)
    assert [
        (lint.source, lint.tier, lint.role)
        for lint in lints
        if lint.code == "unknown-root"
    ] == [("b.yaml", "boundary", None), ("c.yaml", "capability", None)]


def test_when_one_role_carries_an_unknown_root_then_the_lint_names_it():
    doc = {
        "constraints": ["caller.team == 1"],
        "roles": {
            "default": {"tools": {"refund": {"mode": "allow"}}},
            "admin": {"constraints": ['role.name == "x"']},
        },
    }
    lints = analyze_policy(load_policy_set_from_dict(doc))
    assert [
        (lint.message.split(": ")[0], lint.role)
        for lint in lints
        if lint.code == "unknown-root"
    ] == [
        ("a constraint reads caller.team", None),
        ("a constraint in role 'admin' reads role.name", "admin"),
    ]


@pytest.mark.parametrize(
    "constraint", ["count(consts.xs) > 0", "any(consts.xs, . == 1)"]
)
def test_when_a_collection_is_a_constant_then_the_message_says_so(constraint):
    doc = {
        "consts": {"xs": [1]},
        "tools": {"refund": {"mode": "allow", "constraints": [constraint]}},
    }
    assert _unknown_roots_of(doc) == [
        (
            "warning",
            "a constraint reads consts.xs: count(), every() and any() read a "
            "field path, not a constant; compare against the constant instead, "
            "or inline its list",
        )
    ]
