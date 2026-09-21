"""Tests for the roles x tools authorisation matrix.

The matrix is what a compliance report prints as "controls in place", so these
lean on the properties that make it evidence rather than decoration: it agrees
with the engine on every cell, it is per role (never the caller's permissive
union), and it never renders a grant as reachable when the engine denies it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hexgate.security import (
    AgentPolicy,
    BaseToolPolicy,
    DecisionOutcome,
    FileScope,
    FileToolPolicy,
    ModuleContent,
    PolicySet,
    authorisation_matrix,
    combine_role_verdicts,
    link_policy_set,
    load_policy_map,
    load_policy_set_from_dict,
)


def _allow(constraints=None):
    return BaseToolPolicy(mode="allow", constraints=constraints or [])


def _approval(constraints=None):
    return BaseToolPolicy(mode="approval_required", constraints=constraints or [])


def _one(tool_policy, tool="refund"):
    """A single-role set holding exactly one tool — the rendering fixtures."""
    return PolicySet({"default": AgentPolicy(tools={tool: tool_policy})})


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


# ---------------------------------------------------------------------------
# The three document shapes
# ---------------------------------------------------------------------------


def test_authorisation_matrix_happy_path() -> None:
    """A flat legacy policy is the single ``default`` role, one column wide."""
    policy_set = load_policy_set_from_dict(
        {
            "tools": {
                "lookup_order": {"mode": "allow"},
                "refund_order": {
                    "mode": "approval_required",
                    "constraints": ["args.amount <= 500"],
                },
                "delete_database": {"mode": "deny"},
            }
        }
    )

    matrix = authorisation_matrix(policy_set)

    assert matrix.roles == ("default",)
    assert matrix.tools == ("delete_database", "lookup_order", "refund_order")
    assert matrix.aliased_default is None
    assert matrix.cell("lookup_order", "default").mode == "allow"
    assert matrix.cell("lookup_order", "default").constraint_text is None
    refund = matrix.cell("refund_order", "default")
    assert (refund.mode, refund.constraint_text) == ("approval", "args.amount <= 500")
    assert matrix.cell("delete_database", "default").mode == "deny"
    # ``cells`` is public, and PR 3 indexes it directly: pin the key order.
    assert matrix.cells[("lookup_order", "default")] is matrix.cell(
        "lookup_order", "default"
    )


def test_when_policy_declares_inline_roles_then_each_role_is_a_column() -> None:
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {"tools": {"lookup_order": {"mode": "allow"}}},
                "support": {"tools": {"refund_order": {"mode": "approval_required"}}},
                "billing": {
                    "tools": {
                        "refund_order": {
                            "mode": "allow",
                            "constraints": ["args.amount <= 500"],
                        }
                    }
                },
            }
        }
    )

    matrix = authorisation_matrix(policy_set)

    # ``default`` leads; the named roles follow alphabetically.
    assert matrix.roles == ("default", "billing", "support")
    assert matrix.tools == ("lookup_order", "refund_order")
    assert matrix.cell("refund_order", "billing").mode == "allow"
    assert matrix.cell("refund_order", "support").mode == "approval"


def test_when_policy_is_modular_then_matrix_tabulates_the_folded_grants() -> None:
    """After linking, a cell carries the boundary cap AND the capability grant."""
    ceiling = _mod(
        "org.ceiling",
        "boundary",
        {
            "delete_database": BaseToolPolicy(mode="deny"),
            "refund_order": _allow(["args.amount <= 1000"]),
        },
        default_mode="deny",
    )
    payments = _mod(
        "payments",
        "capability",
        {"refund_order": _allow(['args.currency in ["USD", "EUR"]'])},
    )

    matrix = authorisation_matrix(link_policy_set([ceiling], [payments]).policy_set)

    refund = matrix.cell("refund_order", "default")
    assert refund.mode == "allow"
    # Both layers survive into the rendered text, each parenthesised so the
    # join cannot re-associate an ``or`` inside one of them.
    assert "(args.amount <= 1000)" in refund.constraint_text
    assert '(args.currency in ["USD", "EUR"])' in refund.constraint_text
    assert " and " in refund.constraint_text
    assert matrix.cell("delete_database", "default").mode == "deny"


# ---------------------------------------------------------------------------
# Deny-by-default fill, and the ways a role can be empty
# ---------------------------------------------------------------------------


def test_when_a_tool_is_held_by_one_role_then_the_others_deny_it() -> None:
    """The tool axis is the union, so a grant one role lacks is a visible deny —
    not a gap in the table."""
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {"tools": {"lookup_order": {"mode": "allow"}}},
                "billing": {"tools": {"refund_order": {"mode": "allow"}}},
            }
        }
    )

    matrix = authorisation_matrix(policy_set)

    assert matrix.tools == ("lookup_order", "refund_order")
    assert matrix.cell("refund_order", "default").mode == "deny"
    assert matrix.cell("refund_order", "default").constraint_text is None
    assert matrix.cell("lookup_order", "billing").mode == "deny"


def test_when_a_role_has_no_tools_then_its_whole_column_denies() -> None:
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {"tools": {"lookup_order": {"mode": "allow"}}},
                "observer": {},
            }
        }
    )

    matrix = authorisation_matrix(policy_set)

    assert matrix.roles == ("default", "observer")
    assert matrix.cell("lookup_order", "observer").mode == "deny"
    assert matrix.default_cell("anything_else", "observer").mode == "deny"


def test_when_the_only_role_has_no_tools_then_the_tool_axis_is_empty() -> None:
    matrix = authorisation_matrix(PolicySet({"default": AgentPolicy()}))

    assert matrix.roles == ("default",)
    assert matrix.tools == ()
    assert matrix.cells == {}
    assert matrix.default_cell("anything_else", "default").mode == "deny"


def test_when_default_policy_is_permissive_then_the_fallback_says_so() -> None:
    """The grid must not assert a deny the engine would not return: a role with a
    permissive ``default_policy`` really does allow every tool it never lists."""
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {"tools": {"lookup_order": {"mode": "allow"}}},
                "admin": {
                    "default_policy": {
                        "mode": "allow",
                        "constraints": ["args.amount <= 10"],
                    },
                    "tools": {"delete_database": {"mode": "deny"}},
                },
            }
        }
    )

    matrix = authorisation_matrix(policy_set)

    admin_default = matrix.default_cell("anything_else", "admin")
    assert (admin_default.mode, admin_default.constraint_text) == (
        "allow",
        "args.amount <= 10",
    )
    assert matrix.default_cell("anything_else", "default").mode == "deny"
    # A tool admin never lists falls to that permissive default, exactly as the
    # engine resolves it.
    assert matrix.cell("lookup_order", "admin").mode == "allow"
    assert matrix.cell("delete_database", "admin").mode == "deny"


def test_when_an_agent_key_is_unlisted_then_the_cell_denies_closed_world() -> None:
    """``agent.*`` keys are closed-world: a permissive default never grants one,
    which is also why ``defaults`` does not speak for them."""
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {
                    "default_policy": {"mode": "allow"},
                    "admission": {"mode": "allow"},
                },
                "guest": {"default_policy": {"mode": "allow"}},
            }
        }
    )

    matrix = authorisation_matrix(policy_set)

    assert "agent.run" in matrix.tools
    assert matrix.cell("agent.run", "default").mode == "allow"
    assert matrix.cell("agent.run", "guest").mode == "deny"
    # The permissive fallback is reported for ordinary tools, while an unlisted
    # agent key still denies at the engine.
    assert matrix.default_cell("anything_else", "guest").mode == "allow"
    assert (
        policy_set.evaluate(role="guest", tool="agent.handoff:other", args={}).outcome
        is DecisionOutcome.DENY
    )


def test_when_the_default_role_is_inferred_then_the_matrix_names_it() -> None:
    """A policy declaring no ``default`` gets one aliased in. The column is a
    duplicate of a named role, not a baseline anyone authored, and a report that
    presented it as one would evidence a role the policy never declared."""
    policy_set = load_policy_map(
        {
            "billing": AgentPolicy(tools={"refund_order": _allow()}),
            "support": AgentPolicy(tools={"lookup_order": _allow()}),
        }
    )

    matrix = authorisation_matrix(policy_set)

    assert matrix.aliased_default == "billing"
    assert matrix.roles == ("default", "billing", "support")
    assert matrix.cell("refund_order", "default") == matrix.cell(
        "refund_order", "billing"
    )


# ---------------------------------------------------------------------------
# Agent-level reach keys (agent.run / agent.tool: / agent.handoff:)
#
# The report leans on these, and they reach the grid from the policy layer
# whatever declared them, so they are pinned here independently of whichever
# authoring API ends up registering sub-agents.
# ---------------------------------------------------------------------------


def test_when_a_policy_names_reach_targets_then_they_are_ordinary_rows() -> None:
    """``agent.run`` / ``agent.tool:<t>`` / ``agent.handoff:<t>`` are lowered into
    ``effective_tools``, so they tabulate like any tool — their own mode, their own
    constraint text, one row each."""
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {
                    "admission": {
                        "mode": "allow",
                        "constraints": ['ctx.tenant == "acme"'],
                    },
                    "agents": {
                        "billing_bot": {
                            "mode": "approval_required",
                            "via": ["handoff"],
                            "constraints": ["args.amount <= 500"],
                        },
                        "research_bot": {"mode": "allow", "via": ["tool"]},
                    },
                }
            }
        }
    )

    matrix = authorisation_matrix(policy_set)

    assert matrix.tools == (
        "agent.handoff:billing_bot",
        "agent.run",
        "agent.tool:research_bot",
    )
    admission = matrix.cell("agent.run", "default")
    assert (admission.mode, admission.constraint_text) == (
        "allow",
        'ctx.tenant == "acme"',
    )
    handoff = matrix.cell("agent.handoff:billing_bot", "default")
    assert (handoff.mode, handoff.constraint_text) == ("approval", "args.amount <= 500")
    assert matrix.cell("agent.tool:research_bot", "default").mode == "allow"
    # A target named for one via mode is not reachable through the other, so the
    # key for that mode is simply absent rather than silently permitted.
    assert "agent.tool:billing_bot" not in matrix.tools


def test_when_default_policy_is_permissive_then_it_grants_no_unnamed_reach() -> None:
    """Agent keys are closed-world. A role with ``default_policy: allow`` reaches
    only the agents its policy names — reporting otherwise would evidence a
    delegation path the engine refuses."""
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {
                    "default_policy": {"mode": "allow"},
                    "agents": {"research_bot": {"mode": "allow", "via": ["handoff"]}},
                },
                "guest": {"default_policy": {"mode": "allow"}},
            }
        }
    )

    matrix = authorisation_matrix(policy_set)

    assert matrix.cell("agent.handoff:research_bot", "default").mode == "allow"
    assert matrix.cell("agent.handoff:research_bot", "guest").mode == "deny"
    for role in matrix.roles:
        for key in ("agent.run", "agent.handoff:unnamed", "agent.tool:unnamed"):
            assert matrix.default_cell(key, role).mode == "deny", (role, key)
            assert (
                policy_set.evaluate(role=role, tool=key, args={}).outcome
                is DecisionOutcome.DENY
            ), (role, key)


def test_when_a_role_is_permissive_then_defaults_still_denies_agent_keys() -> None:
    """``defaults`` answers only for ordinary tools; ``default_cell`` takes the key
    so it can refuse an ``agent.*`` one. Pinning the asymmetry: the raw mapping
    says ``allow`` and the accessor must not repeat that for a reach key."""
    policy_set = load_policy_set_from_dict(
        {"roles": {"default": {"default_policy": {"mode": "allow"}}}}
    )

    matrix = authorisation_matrix(policy_set)

    assert matrix.defaults["default"].mode == "allow"
    assert matrix.default_cell("some_unlisted_tool", "default").mode == "allow"
    assert matrix.default_cell("agent.run", "default").mode == "deny"
    assert matrix.default_cell("agent.handoff:x", "default").mode == "deny"
    assert matrix.default_cell("agent.tool:x", "default").mode == "deny"


# ---------------------------------------------------------------------------
# Parity with the dashboard's TypeScript resolver
#
# platform/dashboard/src/lib/policy.ts carries a browser-side inherits/mixin
# resolver. It does NOT draw the Graph tab — that renders the server's
# /policy/graph output — and outside its own tests only parseRolesFromPolicy is
# imported, so the resolver is effectively dead. It is still a worked-through
# answer to the same edge cases, so each is pinned here; where this module
# answers differently the divergence is deliberate and says which side wins.
# ---------------------------------------------------------------------------


def test_when_a_tool_mode_is_mistyped_then_the_whole_document_is_rejected() -> None:
    """**Deliberate divergence from policy.ts.**

    ``readToolMap`` (policy.ts:118-132) drops an entry whose mode is not one of
    the three canonical strings, so a mistyped ``mode: Deny`` silently leaves the
    browser's grid. That is fail-closed in the common case rather than lenient —
    the tool then falls through ``effectiveMode`` to ``default_policy``, which
    ``parsePolicy`` defaults to ``deny`` — but the rule the operator wrote is
    gone, and under a permissive ``default_policy`` the typo reads as a grant.
    Dropping a rule we did not understand is defensible for a live preview over
    half-typed YAML and wrong for an evidence report, so the SDK refuses the
    whole document at load and no report can be generated from it at all.
    """
    with pytest.raises(ValidationError, match="'allow', 'deny' or 'approval_required'"):
        load_policy_set_from_dict({"tools": {"delete_database": {"mode": "Deny"}}})

    # And the tool is never quietly dropped from a document that does load.
    matrix = authorisation_matrix(
        load_policy_set_from_dict({"tools": {"delete_database": {"mode": "deny"}}})
    )
    assert matrix.tools == ("delete_database",)
    assert matrix.cell("delete_database", "default").mode == "deny"


@pytest.mark.parametrize("truthy", [True, "true", "yes", "on", 1])
def test_when_is_mixin_is_a_coerced_truthy_then_the_role_is_not_a_column(
    truthy,
) -> None:
    """Matches ``isMixinSpec`` (policy.ts:83-89), which accepts exactly these —
    the bool plus ``"true"``/``"yes"``/``"on"``/``1``. A mixin that slipped
    through as concrete would add a column for a role no caller can be assigned.
    """
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {"tools": {"lookup_order": {"mode": "allow"}}},
                "read_only": {
                    "is_mixin": truthy,
                    "tools": {"refund_order": {"mode": "allow"}},
                },
            }
        }
    )

    matrix = authorisation_matrix(policy_set)

    assert matrix.roles == ("default",)
    # Its grants are absent too — nothing inherits it here, so the tool it
    # carried is not reachable by anyone.
    assert matrix.tools == ("lookup_order",)


@pytest.mark.parametrize("truthy", ["True", "TRUE", "Yes", "On", "1"])
def test_when_is_mixin_is_a_case_variant_then_the_sdk_is_stricter(truthy) -> None:
    """**Deliberate divergence from policy.ts.**

    ``isMixinSpec`` compares with ``===`` against lowercase literals, so
    ``is_mixin: "True"`` reads as a *concrete role* in the browser while
    pydantic's bool coercion makes it a mixin here. The SDK is the side to
    follow: a policy that declares a mixin cannot have it assigned to a caller,
    so publishing a column for it would evidence a role nobody can hold.
    """
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {"tools": {"lookup_order": {"mode": "allow"}}},
                "read_only": {"is_mixin": truthy, "tools": {}},
            }
        }
    )

    assert authorisation_matrix(policy_set).roles == ("default",)


def test_when_a_concrete_role_has_no_tools_then_it_still_gets_a_column() -> None:
    """Matches policy.ts:272 for the ``{}`` spelling, which publishes a concrete
    role's map even when empty — dropping the role would read as "not covered"
    rather than "grants nothing".

    **Divergence on the ``observer:`` spelling**, which ``yaml.safe_load`` gives
    as ``None``: policy.ts:253 skips a non-object spec, so that role vanishes
    from its grid, while the SDK still publishes it. The SDK is the side to
    follow for a report — and it also matches what ``parseRolesFromPolicy``
    offers in the Playground picker, which has no such guard.
    """
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {"tools": {"lookup_order": {"mode": "allow"}}},
                "observer": None,
                "auditor": {"tools": {}},
            }
        }
    )

    matrix = authorisation_matrix(policy_set)

    assert matrix.roles == ("default", "auditor", "observer")
    for role in ("auditor", "observer"):
        assert matrix.cell("lookup_order", role).mode == "deny"
        assert matrix.default_cell("anything_else", role).mode == "deny"


# ---------------------------------------------------------------------------
# Per role, not the caller's union
# ---------------------------------------------------------------------------


def test_when_roles_disagree_then_cells_stay_per_role() -> None:
    """``combine_role_verdicts`` takes the permissive union at call time; the
    matrix deliberately does not — each column is that role's own standing grant."""
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {},
                "support": {"tools": {"refund_order": {"mode": "approval_required"}}},
                "billing": {"tools": {"refund_order": {"mode": "allow"}}},
            }
        }
    )

    matrix = authorisation_matrix(policy_set)

    assert matrix.cell("refund_order", "support").mode == "approval"
    assert matrix.cell("refund_order", "billing").mode == "allow"
    # The same caller holding both roles is allowed outright — the union the
    # matrix does not show.
    verdict, _ = combine_role_verdicts(
        ["support", "billing"],
        lambda role: policy_set.evaluate(role=role, tool="refund_order", args={}),
    )
    assert verdict.outcome is DecisionOutcome.ALLOW


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_policy,expected",
    [
        pytest.param(_allow(), None, id="unconditional"),
        pytest.param(
            _allow(["args.amount <= 500"]), "args.amount <= 500", id="verbatim"
        ),
        pytest.param(
            _approval(['args.currency == "EUR" or args.amount <= 10', "args.x > 0"]),
            '(args.currency == "EUR" or args.amount <= 10) and (args.x > 0)',
            id="parenthesised-when-joined",
        ),
        pytest.param(
            BaseToolPolicy(mode="deny", constraints=["args.amount <= 500"]),
            None,
            id="deny-drops-its-constraints",
        ),
    ],
)
def test_constraint_text_rendering(tool_policy, expected) -> None:
    """A single constraint renders verbatim; several are parenthesised so an
    ``or`` inside one cannot re-associate across the join. A deny renders none:
    the engine short-circuits before reading them, so printing one would suggest
    the tool becomes reachable when the constraint holds."""
    assert (
        authorisation_matrix(_one(tool_policy))
        .cell("refund", "default")
        .constraint_text
        == expected
    )


def test_when_a_grant_has_file_scope_then_the_cell_names_the_path_argument() -> None:
    """A path-fenced tool must not read as an unconditional allow."""
    policy_set = _one(
        FileToolPolicy(
            mode="allow",
            file_scope=FileScope(
                allowed_paths=["data/**"], denied_paths=["data/secrets/**"]
            ),
        ),
        tool="read_file",
    )

    cell = authorisation_matrix(policy_set).cell("read_file", "default")
    assert cell.mode == "allow"
    assert cell.constraint_text == (
        "file_scope: file_path must be present, within ['data/**'], "
        "outside ['data/secrets/**']"
    )


def test_when_file_scope_is_empty_then_the_path_requirement_is_still_named() -> None:
    """``is_path_allowed`` denies a call whose path argument is missing, so a
    present-but-empty block is a restriction, not a no-op."""
    policy_set = _one(
        FileToolPolicy(mode="allow", file_scope=FileScope()), tool="read_file"
    )

    cell = authorisation_matrix(policy_set).cell("read_file", "default")
    assert cell.constraint_text == "file_scope: file_path must be present"


def test_when_a_grant_has_both_a_constraint_and_file_scope_then_both_are_joined() -> (
    None
):
    """The lone constraint gets parenthesised once the scope clause joins it, so
    an ``or`` inside it cannot re-associate across the ``and``."""
    policy_set = _one(
        FileToolPolicy(
            mode="allow",
            constraints=['args.encoding == "utf-8" or args.encoding == "ascii"'],
            file_scope=FileScope(allowed_paths=["data/**"]),
        ),
        tool="read_file",
    )

    text = authorisation_matrix(policy_set).cell("read_file", "default").constraint_text
    assert text == (
        '(args.encoding == "utf-8" or args.encoding == "ascii") and '
        "file_scope: file_path must be present, within ['data/**']"
    )


def test_when_file_scope_is_on_an_unscopable_tool_then_the_cell_denies() -> None:
    """``is_path_allowed`` reads the path out of one argument chosen by tool name
    and denies outright for a tool it has no entry for. Rendering that as an
    allow under a satisfiable-looking condition would evidence a grant that is
    really a block."""
    policy_set = _one(
        FileToolPolicy(mode="allow", file_scope=FileScope(allowed_paths=["data/**"])),
        tool="download_report",
    )

    cell = authorisation_matrix(policy_set).cell("download_report", "default")
    assert cell.mode == "deny"
    assert cell.constraint_text is None
    # ... which is what the engine does, for every shape of argument.
    for args in ({}, {"file_path": "data/x"}, {"path": "data/x"}, {"url": "data/x"}):
        verdict = policy_set.evaluate(role=None, tool="download_report", args=args)
        assert verdict.outcome is DecisionOutcome.DENY


def test_when_a_role_has_a_policy_wide_fence_then_every_cell_carries_it() -> None:
    """``AgentPolicy.constraints`` gate every tool the role can reach, so a cell
    that omitted them would read as a looser grant than the engine enforces."""
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {
                    "constraints": ["run.tool_calls < 50"],
                    "default_policy": {"mode": "allow"},
                    "tools": {
                        "refund_order": {
                            "mode": "allow",
                            "constraints": ["args.amount <= 500"],
                        },
                        "escalate": {
                            "mode": "approval_required",
                            "constraints": ["args.tier == 2"],
                        },
                        "read_file": {
                            "mode": "allow",
                            "file_scope": {"allowed_paths": ["data/**"]},
                        },
                        "lookup_order": {"mode": "allow"},
                        "delete_database": {"mode": "deny"},
                    },
                }
            }
        }
    )

    matrix = authorisation_matrix(policy_set)

    # The fence leads, in the order evaluate_tool_call concatenates it.
    assert matrix.cell("refund_order", "default").constraint_text == (
        "(run.tool_calls < 50) and (args.amount <= 500)"
    )
    # A tool with no constraints of its own still carries the fence, alone and
    # therefore unparenthesised.
    assert (
        matrix.cell("lookup_order", "default").constraint_text == "run.tool_calls < 50"
    )
    # And so does the permissive fallback, which is also gated by it.
    assert (
        matrix.default_cell("anything_else", "default").constraint_text
        == "run.tool_calls < 50"
    )
    # An approval cell is gated by the fence exactly as an allow cell is — the
    # engine checks constraints before it reports NEEDS_APPROVAL.
    escalate = matrix.cell("escalate", "default")
    assert (escalate.mode, escalate.constraint_text) == (
        "approval",
        "(run.tool_calls < 50) and (args.tier == 2)",
    )
    # And so is a file-scoped cell, whose scope clause joins the fence.
    assert matrix.cell("read_file", "default").constraint_text == (
        "(run.tool_calls < 50) and file_scope: file_path must be present, "
        "within ['data/**']"
    )
    # A deny short-circuits before any constraint, fence included.
    assert matrix.cell("delete_database", "default").constraint_text is None


def test_when_a_fence_is_inherited_then_the_union_reaches_the_cell() -> None:
    """Policy-level constraints union across ``inherits``; the matrix reads the
    resolved policy, so both the mixin's fence and the role's own appear."""
    policy_set = load_policy_map(
        {
            "base": AgentPolicy(is_mixin=True, constraints=["run.tool_calls < 50"]),
            "default": AgentPolicy(
                inherits=["base"],
                constraints=["run.elapsed_seconds < 300"],
                tools={"refund_order": _allow()},
            ),
        }
    )

    text = authorisation_matrix(policy_set).cell("refund_order", "default")
    assert text.constraint_text == (
        "(run.tool_calls < 50) and (run.elapsed_seconds < 300)"
    )


def test_when_a_cell_is_off_grid_then_it_raises() -> None:
    """A misspelled tool must not quietly evidence a deny nobody looked up."""
    matrix = authorisation_matrix(_one(_allow()))

    with pytest.raises(KeyError):
        matrix.cell("refnud", "default")
    with pytest.raises(KeyError):
        matrix.cell("refund", "billing")


# ---------------------------------------------------------------------------
# Agreement with the engine
# ---------------------------------------------------------------------------

# Argument shapes a caller could plausibly send, spanning the ones the fixture's
# constraints and file scopes accept and reject. Used to probe a cell's claim
# against the engine from both sides.
_ARG_PROBES = (
    {},
    {"amount": 10},
    {"amount": 10_000},
    {"file_path": "data/report.csv"},
    {"file_path": "data/secrets/key.pem"},
    {"path": "data/report.csv"},
    # Both at once: a role-wide fence on args.amount gates the file tools too,
    # so a probe set that only ever varies one argument would read every
    # file-tool cell as unreachable.
    {"file_path": "data/report.csv", "amount": 10},
)

# A role-wide fence is as likely to read ``run.*`` or ``ctx.*`` as ``args.*``,
# and those namespaces are supplied separately from the arguments. Probing only
# args would report a cell as unreachable whenever a fence names either — which
# would fail against correct code the moment a fence like the one in
# ``test_when_a_role_has_a_policy_wide_fence_then_every_cell_carries_it`` is
# added to the fixture below.
_CTX_PROBES = ({}, {"tenant": "acme"})
_RUN_PROBES = ({}, {"tool_calls": 1}, {"tool_calls": 10_000})


@pytest.fixture
def engine_fixture():
    return load_policy_set_from_dict(
        {
            "roles": {
                "default": {"tools": {"lookup_order": {"mode": "allow"}}},
                "billing": {
                    "constraints": ["args.amount <= 5000", "run.tool_calls < 50"],
                    "default_policy": {"mode": "allow"},
                    "tools": {
                        "refund_order": {
                            "mode": "approval_required",
                            "constraints": ["args.amount <= 500"],
                        },
                        "read_file": {
                            "mode": "allow",
                            "file_scope": {
                                "allowed_paths": ["data/**"],
                                "denied_paths": ["data/secrets/**"],
                            },
                        },
                        "fetch_doc": {
                            "mode": "allow",
                            "file_scope": {"allowed_paths": ["data/**"]},
                        },
                        "delete_database": {"mode": "deny"},
                    },
                },
                "observer": {},
            }
        }
    )


@pytest.mark.parametrize("role", ["default", "billing", "observer", "unknown_role"])
def test_every_cell_agrees_with_the_pydantic_engine(engine_fixture, role: str) -> None:
    """A cell's mode must not claim more than the engine grants.

    ``deny`` has to mean the engine denies *every* argument shape, and any other
    mode has to mean some argument shape gets through — which is what catches a
    grant rendered as reachable when nothing can satisfy it.
    """
    matrix = authorisation_matrix(engine_fixture)
    # An undefined role resolves to ``default`` in both the matrix and the engine.
    column = role if role in matrix.roles else "default"

    for tool in matrix.tools:
        outcomes = {
            engine_fixture.evaluate(
                role=role, tool=tool, args=args, attributes=ctx, run=run
            ).outcome
            for args in _ARG_PROBES
            for ctx in _CTX_PROBES
            for run in _RUN_PROBES
        }
        cell = matrix.cell(tool, column)
        expected = {
            "allow": DecisionOutcome.ALLOW,
            "approval": DecisionOutcome.NEEDS_APPROVAL,
        }
        if cell.mode == "deny":
            assert outcomes == {DecisionOutcome.DENY}, tool
        elif cell.constraint_text is None:
            # The cell claims the grant is unconditional, so every argument
            # shape must get it. This is what catches a condition the renderer
            # dropped — a mode-only check cannot, however many probes it runs.
            assert outcomes == {expected[cell.mode]}, tool
        else:
            assert outcomes - {DecisionOutcome.DENY}, tool
            assert expected[cell.mode] in outcomes, tool
