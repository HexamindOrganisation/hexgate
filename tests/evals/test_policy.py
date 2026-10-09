"""Loading, validating and dry-running a policy (`policy.py`), on synthetic workspaces."""

from __future__ import annotations

import datetime
import json
from typing import get_args

import pytest

from evals.policy_writing.policy import (
    DRIFT_CODES,
    CaseError,
    agent_policies,
    decide,
    effective_policy,
    outcome,
)
from hexgate.security.analyzer import LintCode
from hexgate.security.decision import DecisionOutcome
from tests.evals.helpers import (
    AGENT,
    OPS_COLUMN_PERMISSIVE_DEFAULT,
    PERMISSIVE_DEFAULT,
    POLICY,
    make_modules_workspace,
    make_workspace,
    valid_policy,
)

# Reach declared for handoff only, not for agent-as-tool.
HANDOFF_ONLY = """\
version: 1
roles:
  default:
    tools:
      view_orders: { mode: allow }
    agents:
      billing-bot: { mode: allow, via: [handoff] }
"""

DATED_CONSTRAINT = """\
version: 1
roles:
  default:
    tools:
      view_orders:
        mode: allow
        constraints: ['args.since >= "2026-01-01"', 'ctx.hired_on >= "2026-01-01"']
"""


# Roles disagreeing on a guard: one guard pipeline per agent can't serve both.
GUARD_DIVERGENCE = """\
version: 1
roles:
  default:
    guards:
      g: { enabled: false }
  admin:
    guards:
      g: { enabled: true }
"""


# Agent gates whose constraints read the args only the gate sends.
ADMIT_SHOP_BOT = """\
version: 1
roles:
  default:
    admission: { mode: allow, constraints: ['args.agent == "shop-bot"'] }
"""

HANDOFF_TO_BILLING = """\
version: 1
roles:
  default:
    agents:
      billing-bot:
        mode: allow
        constraints: ['args.target == "billing-bot"', 'args.via == "handoff"']
"""


# A skill rule reading the args only the skill gate sends.
PDF_INSTRUCTIONS = """\
version: 1
roles:
  default:
    skills:
      pdf: { mode: allow, constraints: ['args.via == "instructions"', 'args.skill == "pdf"'] }
"""

PDF_FORMS_ONLY = """\
version: 1
roles:
  default:
    skills:
      pdf: { mode: allow, constraints: ['args.file_path == "forms.md"'] }
"""

# A rule on the agent the run belongs to.
SHOP_BOT_RUN = """\
version: 1
roles:
  default:
    tools:
      view_orders: { mode: allow, constraints: ['run.agent == "shop-bot"'] }
"""


# A per-tool cap on a reach rule, which the runtime never counts.
HANDOFF_ONCE = """\
version: 1
roles:
  default:
    agents:
      billing-bot: { mode: allow, constraints: ['run.calls_of_this_tool < 1'] }
"""

# Admission decided by a rule on the agent's run.
ADMIT_IN_RUN = """\
version: 1
roles:
  default:
    admission: { mode: allow, constraints: ['run.agent == "shop-bot"'] }
"""

# Egress decided by a rule on the agent's run.
EGRESS_IN_RUN = """\
version: 1
roles:
  default:
    tools:
      net.http_request: { mode: allow, constraints: ['run.agent == "shop-bot"'] }
"""


@pytest.mark.parametrize(
    ("role", "amount", "expected"),
    [
        ("billing", 500, DecisionOutcome.ALLOW),
        ("billing", 501, DecisionOutcome.DENY),
        ("support", 10, DecisionOutcome.NEEDS_APPROVAL),
    ],
)
def test_decide_happy_path(tmp_path, role, amount, expected) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    call = {"tool": "refund_order", "args": {"order_id": "o1", "amount": amount}}
    verdict = decide(policy, role, call)
    assert verdict.outcome == expected, verdict.reason


@pytest.mark.parametrize("tool", ["agent.run", "agent.handoff:ops-bot"])
def test_when_admission_or_handoff_is_not_declared_then_decide_allows(
    tmp_path, tool
) -> None:
    # As the runtime: the gate checks nothing, and the call goes through.
    policy = valid_policy(make_workspace(tmp_path))
    assert decide(policy, "default", {"tool": tool}).outcome == DecisionOutcome.ALLOW


@pytest.mark.parametrize("tool", ["agent.tool:ops-bot", "skill:pdf"])
def test_when_a_tool_reach_or_skill_gate_is_not_declared_then_decide_raises(
    tmp_path, tool
) -> None:
    # The runtime decides such a call under the tool's own name instead.
    policy = valid_policy(make_workspace(tmp_path))
    with pytest.raises(CaseError, match="dry-run that tool instead"):
        decide(policy, "default", {"tool": tool})


def test_when_a_gate_is_declared_then_decide_evaluates_it(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path, HANDOFF_ONLY))
    handoff = decide(policy, "default", {"tool": "agent.handoff:ops-bot"})
    assert handoff.outcome == DecisionOutcome.DENY  # declared, and ops-bot unlisted
    with pytest.raises(CaseError, match="agent-as-tool reach"):
        decide(policy, "default", {"tool": "agent.tool:ops-bot"})


def test_when_a_call_holds_yaml_dates_then_decide_compares_them_as_text(
    tmp_path,
) -> None:
    # As `policy test --args` / `--attributes` (JSON) would give them.
    policy = valid_policy(make_workspace(tmp_path, DATED_CONSTRAINT))
    february, december = datetime.date(2026, 2, 1), datetime.date(2025, 12, 1)
    call = {"tool": "view_orders", "args": {"since": february}}
    for hired_on, expected in [
        (february, DecisionOutcome.ALLOW),
        (december, DecisionOutcome.DENY),
    ]:
        dated = {**call, "attributes": {"hired_on": hired_on}}
        assert decide(policy, "default", dated).outcome == expected


def test_when_a_call_is_on_admission_then_decide_sends_the_agent(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path, ADMIT_SHOP_BOT), AGENT)
    verdict = decide(policy, "default", {"tool": "agent.run"})
    assert verdict.outcome == DecisionOutcome.ALLOW, verdict.reason


@pytest.mark.parametrize(
    "tool", ["agent.handoff:billing-bot", "agent.handoff: billing-bot "]
)
def test_when_a_call_is_on_reach_then_decide_sends_the_trimmed_target_and_via(
    tmp_path, tool
) -> None:
    policy = valid_policy(make_workspace(tmp_path, HANDOFF_TO_BILLING), AGENT)
    verdict = decide(policy, "default", {"tool": tool})
    assert verdict.outcome == DecisionOutcome.ALLOW, verdict.reason


def test_when_a_gate_call_carries_its_own_args_then_decide_raises(tmp_path) -> None:
    # The gate sends its own args, so a case's would be silently ignored.
    policy = valid_policy(make_workspace(tmp_path, ADMIT_SHOP_BOT), AGENT)
    with pytest.raises(CaseError, match=r"drop \['agent'\]"):
        decide(policy, "default", {"tool": "agent.run", "args": {"agent": "ops-bot"}})


def test_when_an_undeclared_admission_call_carries_args_then_decide_allows_it(
    tmp_path,
) -> None:
    policy = valid_policy(make_workspace(tmp_path), AGENT)
    call = {"tool": "agent.run", "args": {"agent": "ops-bot"}}
    assert decide(policy, "default", call).outcome == DecisionOutcome.ALLOW


def test_when_an_undeclared_tool_reach_call_carries_args_then_decide_says_undeclared(
    tmp_path,
) -> None:
    policy = valid_policy(make_workspace(tmp_path), AGENT)
    with pytest.raises(CaseError, match="isn't declared"):
        decide(policy, "default", {"tool": "agent.tool:ops-bot", "args": {"x": 1}})


def test_when_a_gate_key_is_called_in_a_run_then_its_own_count_stays_zero(
    tmp_path,
) -> None:
    # As at runtime, where the cap never fires.
    policy = valid_policy(make_workspace(tmp_path, HANDOFF_ONCE), AGENT)
    call = {"tool": "agent.handoff:billing-bot", "run_facts": {"tool_calls": 5}}
    verdict = decide(policy, "default", call)
    assert verdict.outcome == DecisionOutcome.ALLOW, verdict.reason


@pytest.mark.parametrize(
    ("text", "call"),
    [
        (ADMIT_IN_RUN, {"tool": "agent.run"}),
        (EGRESS_IN_RUN, {"tool": "net.http_request", "args": {"host": "example.com"}}),
    ],
)
def test_when_a_call_is_decided_outside_any_run_then_decide_sees_no_run(
    tmp_path, text, call
) -> None:
    # Admission before the run starts, egress above every run: `run.agent` is "".
    policy = valid_policy(make_workspace(tmp_path, text), AGENT)
    assert decide(policy, "default", call).outcome == DecisionOutcome.DENY


def test_when_a_run_fact_key_is_not_a_string_then_decide_raises(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    with pytest.raises(CaseError):
        decide(policy, "default", {"tool": "view_orders", "run_facts": {1: 2}})


@pytest.mark.parametrize("tool", ["skill:pdf", "skill: pdf "])
def test_when_a_call_is_on_a_skill_gate_then_decide_sends_the_trimmed_skill_and_via(
    tmp_path, tool
) -> None:
    policy = valid_policy(make_workspace(tmp_path, PDF_INSTRUCTIONS))
    verdict = decide(policy, "default", {"tool": tool})
    assert verdict.outcome == DecisionOutcome.ALLOW, verdict.reason


def test_when_a_skill_call_gives_a_file_path_then_decide_passes_it(tmp_path) -> None:
    # The skill gate takes `file_path` from the call; one the case leaves out is None.
    policy = valid_policy(make_workspace(tmp_path, PDF_FORMS_ONLY))
    for args, expected in [
        ({"file_path": "forms.md"}, DecisionOutcome.ALLOW),
        ({}, DecisionOutcome.DENY),
    ]:
        call = {"tool": "skill.resource:pdf", "args": args}
        assert decide(policy, "default", call).outcome == expected


def test_when_a_skill_call_sets_what_the_gate_sends_then_decide_raises(
    tmp_path,
) -> None:
    policy = valid_policy(make_workspace(tmp_path, PDF_INSTRUCTIONS))
    with pytest.raises(CaseError, match=r"drop \['via'\]"):
        decide(policy, "default", {"tool": "skill:pdf", "args": {"via": "script"}})


def test_when_a_skill_call_spells_the_gate_args_it_sends_then_decide_accepts_them(
    tmp_path,
) -> None:
    # As the case loader (PR 2) completes a skill call.
    policy = valid_policy(make_workspace(tmp_path, PDF_INSTRUCTIONS))
    call = {"tool": "skill:pdf", "args": {"skill": "pdf", "via": "instructions"}}
    assert decide(policy, "default", call).outcome == DecisionOutcome.ALLOW


def test_when_a_rule_reads_run_agent_then_decide_gives_the_case_agent(
    tmp_path,
) -> None:
    ws = make_workspace(tmp_path, SHOP_BOT_RUN)
    for agent, expected in [
        (AGENT, DecisionOutcome.ALLOW),
        ("ops-bot", DecisionOutcome.DENY),
    ]:
        policy = valid_policy(ws, agent)
        assert decide(policy, "default", {"tool": "view_orders"}).outcome == expected


@pytest.mark.parametrize(
    ("tool", "facts"),
    [
        ("agent.run", {"tool_calls": 1}),
        ("net.http_request", {"tool_calls": 1}),
        ("view_orders", {"agent": "ops-bot"}),
        ("agent.handoff:billing-bot", {"calls_of_this_tool": 1}),
    ],
)
def test_when_run_facts_contradict_the_run_then_decide_raises(
    tmp_path, tool, facts
) -> None:
    policy = valid_policy(make_workspace(tmp_path), AGENT)
    with pytest.raises(CaseError, match="drop run_facts"):
        decide(policy, "default", {"tool": tool, "run_facts": facts})


@pytest.mark.parametrize("facts", [{"tool": 1}, {"tool_call": 20}])
def test_when_a_run_fact_is_unknown_then_decide_raises(tmp_path, facts) -> None:
    # `tool` would collide with `run_namespace`'s own parameter.
    policy = valid_policy(make_workspace(tmp_path))
    with pytest.raises(CaseError, match="unknown run"):
        decide(policy, "default", {"tool": "view_orders", "run_facts": facts})


def test_when_the_role_is_undefined_then_decide_raises(tmp_path) -> None:
    # Not the `default` fallback: a case naming a role the policy lacks fails.
    policy = valid_policy(make_workspace(tmp_path))
    with pytest.raises(CaseError, match="suport"):
        decide(policy, "suport", {"tool": "view_orders"})


def test_outcome_happy_path() -> None:
    assert outcome("approval_required") == DecisionOutcome.NEEDS_APPROVAL


def test_effective_policy_happy_path(tmp_path) -> None:
    policy, problems = effective_policy(make_workspace(tmp_path))
    assert problems == []
    assert policy is not None


def test_when_a_lint_warns_then_effective_policy_fails(tmp_path) -> None:
    policy, problems = effective_policy(make_workspace(tmp_path, PERMISSIVE_DEFAULT))
    assert policy is None
    assert any("permissive-default" in p for p in problems)


def test_when_the_policy_holds_a_yaml_date_then_effective_policy_fails(
    tmp_path,
) -> None:
    dated = "version: 1\nroles:\n  default:\n    consts:\n      cutoff: 2026-01-01\n"
    policy, problems = effective_policy(make_workspace(tmp_path, dated))
    assert policy is None
    assert problems[0].startswith("can't compile:")


def test_when_roles_disagree_on_guards_then_effective_policy_fails(tmp_path) -> None:
    # As `hexgate policy validate` rejects it.
    policy, problems = effective_policy(make_workspace(tmp_path, GUARD_DIVERGENCE))
    assert policy is None
    assert any("guard-divergence" in p for p in problems)


def test_when_policy_yaml_is_empty_then_it_is_an_empty_policy(tmp_path) -> None:
    # As `hexgate policy validate` reads it: valid, and every call denied.
    policy = valid_policy(make_workspace(tmp_path, "# nothing yet\n"))
    verdict = decide(policy, "default", {"tool": "view_orders"})
    assert verdict.outcome == DecisionOutcome.DENY


def test_when_policy_yaml_is_not_utf8_then_effective_policy_fails(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    (ws / "policy.yaml").write_bytes("# café\nversion: 1\n".encode("latin-1"))
    policy, problems = effective_policy(ws)
    assert policy is None
    assert "utf-8" in problems[0]


def test_effective_policy_happy_path_on_a_module_tree(tmp_path) -> None:
    policy = valid_policy(make_modules_workspace(tmp_path), modules=True)
    refund = {"tool": "refund_order", "args": {"amount": 1001}}
    verdict = decide(policy, "billing", refund)
    assert verdict.outcome == DecisionOutcome.DENY  # the boundary's cap


def test_when_the_agent_has_its_own_column_then_effective_policy_resolves_it(
    tmp_path,
) -> None:
    roles = '  billing:\n    "*": [read_only]\n    shop-bot: [read_only, payments]\n'
    ws = make_modules_workspace(tmp_path, roles)
    refund = {"tool": "refund_order", "args": {"amount": 5}}
    for agent, expected in [
        (AGENT, DecisionOutcome.ALLOW),
        ("ops-bot", DecisionOutcome.DENY),
    ]:
        policy = valid_policy(ws, agent, modules=True)
        assert decide(policy, "billing", refund).outcome == expected


def test_when_a_module_tree_has_a_permissive_default_then_effective_policy_fails(
    tmp_path,
) -> None:
    # `policy check` doesn't lint the composed roles; only the resolved policy
    # shows that `default` grants what no named role does.
    roles = "  default: [read_only, payments]\n  billing: [read_only]\n"
    _, problems = effective_policy(
        make_modules_workspace(tmp_path, roles), modules=True
    )
    assert any("permissive-default" in p for p in problems)


def test_when_a_module_tree_has_a_dead_grant_then_effective_policy_fails(
    tmp_path,
) -> None:
    # A module lint: the boundary denies what a capability grants.
    ws = make_modules_workspace(tmp_path)
    (ws / "policies" / "boundaries" / "no_views.yaml").write_text(
        "tools:\n  view_orders: { mode: deny }\n"
    )
    _, problems = effective_policy(ws, modules=True)
    assert any("dead-grant" in p for p in problems)


def test_when_a_roles_column_names_no_agent_in_agents_json_then_effective_policy_fails(
    tmp_path,
) -> None:
    # A misspelled column: shop-bot falls back to `"*"`, which grants refunds.
    roles = '  billing:\n    "*": [read_only, payments]\n    shop-bott: [read_only]\n'
    ws = make_modules_workspace(tmp_path, roles)
    _, problems = effective_policy(ws, AGENT, modules=True)
    assert any(
        p.startswith("[unknown-agent] [billing] [agent shop-bott] ") for p in problems
    )


def test_when_a_reach_target_names_no_agent_in_agents_json_then_effective_policy_fails(
    tmp_path,
) -> None:
    roles = "  default: [read_only]\n  billing: [read_only, reach]\n"
    ws = make_modules_workspace(tmp_path, roles)
    (ws / "policies" / "capabilities" / "reach.yaml").write_text(
        "agents:\n  opps-bot: { mode: allow }\n"
    )
    _, problems = effective_policy(ws, AGENT, modules=True)
    assert any("[unknown-reach-target]" in p for p in problems)


def test_when_agents_json_is_missing_then_effective_policy_fails_on_a_module_tree(
    tmp_path,
) -> None:
    ws = make_modules_workspace(tmp_path)
    (ws / "agents.json").unlink()
    _, problems = effective_policy(ws, AGENT, modules=True)
    assert len(problems) == 1 and problems[0].startswith("agents.json unreadable")


# Policy.drift: the SDK's manifest lints, for the name checks


def test_every_drift_code_is_one_the_sdk_emits() -> None:
    # No type checker runs here, so a misspelled or renamed code would route
    # nothing: pin it against the SDK's own list.
    assert DRIFT_CODES <= set(get_args(LintCode))


def _drop_draft_bot(ws) -> None:
    """Leave only agents with a manifest, so `"*"` cells and boundaries are checked."""
    views = json.loads((ws / "agents.json").read_text())
    (ws / "agents.json").write_text(json.dumps([v for v in views if v["manifest"]]))


def test_when_a_policy_file_invents_a_tool_then_drift_holds_it_and_valid_passes(
    tmp_path,
) -> None:
    ws = make_workspace(tmp_path, POLICY + "      refnd_order: { mode: allow }\n")
    policy = valid_policy(ws, AGENT)
    assert [(x.code, x.tool) for x in policy.drift] == [("unknown-tool", "refnd_order")]


def test_when_no_agent_is_given_then_no_manifest_and_no_drift(tmp_path) -> None:
    ws = make_workspace(tmp_path, POLICY + "      refnd_order: { mode: allow }\n")
    policy = valid_policy(ws)
    assert (policy.manifest, policy.drift) == (None, [])


@pytest.mark.parametrize("without_manifest", [None, "draft-bot", "sub-agent"])
def test_when_a_star_cell_has_a_typo_then_drift_holds_it(
    tmp_path, without_manifest
) -> None:
    # An agent with no manifest, or a sub-agent with none, adds no
    # names: the `"*"` cell is still checked against the rest.
    ws = make_modules_workspace(tmp_path)
    views = json.loads((ws / "agents.json").read_text())
    if without_manifest != "draft-bot":
        views = [v for v in views if v["manifest"]]
    if without_manifest == "sub-agent":
        views[0]["manifest"]["subagents"] = [{"name": "helper-bot", "via": "tool"}]
    (ws / "agents.json").write_text(json.dumps(views))
    (ws / "policies" / "capabilities" / "payments.yaml").write_text(
        'tools:\n  refnd_order: { mode: allow }\n  refund_order: { mode: allow, constraints: ["args.amout < 5"] }\n'
    )
    drift = valid_policy(ws, AGENT, modules=True).drift
    assert {(x.code, x.tool) for x in drift} == {
        ("unknown-tool", "refnd_order"),
        ("unknown-arg", "refund_order"),
    }


def test_when_the_manifest_lists_null_skills_then_an_invented_skill_is_drift(
    tmp_path,
) -> None:
    # The agent lists no skills: none is known.
    ws = make_workspace(tmp_path, "version: 1\nskills:\n  pdf: { mode: allow }\n")
    views = json.loads((ws / "agents.json").read_text())
    views[0]["manifest"]["skills"] = None
    (ws / "agents.json").write_text(json.dumps(views))
    assert [x.code for x in valid_policy(ws, AGENT).drift] == ["unknown-skill"]


@pytest.mark.parametrize(
    ("module", "text"),
    [
        # An org-wide deny on ops-bot's tool and skill.
        (
            "boundaries/org.yaml",
            "default_policy: { mode: allow }\ntools:\n  wire_transfer: { mode: deny }\n"
            "skills:\n  ledger: { mode: deny }\n",
        ),
        # A `"*"` cell's capability may grant any agent's tool.
        ("capabilities/payments.yaml", "tools:\n  wire_transfer: { mode: allow }\n"),
    ],
)
def test_when_a_module_tree_names_another_agents_tool_then_no_drift(
    tmp_path, module, text
) -> None:
    ws = make_modules_workspace(tmp_path)
    _drop_draft_bot(ws)
    (ws / "policies" / module).write_text(text)
    assert valid_policy(ws, AGENT, modules=True).drift == []


# agent_policies

COLUMNS = '  billing:\n    "*": [read_only]\n    ops-bot: [read_only, payments]\n'


def test_agent_policies_happy_path(tmp_path) -> None:
    ws = make_modules_workspace(tmp_path, COLUMNS)
    policies, problems = agent_policies(ws, None, valid_policy(ws, None, True), True)
    # One per agent in agents.json, plus "*" for one not registered yet.
    assert (sorted(policies), problems) == (
        ["*", "draft-bot", "ops-bot", "shop-bot"],
        [],
    )
    assert policies["ops-bot"].agent == "ops-bot"  # its gates send its name


def test_when_the_case_is_not_role_wide_then_there_is_one_policy(tmp_path) -> None:
    # An agent case: its own policy only.
    ws = make_modules_workspace(tmp_path, COLUMNS)
    policy = valid_policy(ws, AGENT, True)
    assert agent_policies(ws, AGENT, policy, role_wide=False) == ({AGENT: policy}, [])


def test_when_an_agents_policy_is_invalid_then_agent_policies_returns_none(
    tmp_path,
) -> None:
    ws = make_modules_workspace(tmp_path, OPS_COLUMN_PERMISSIVE_DEFAULT)
    policies, problems = agent_policies(ws, None, valid_policy(ws, None, True), True)
    # None, rather than the valid ones: an agent left out would pass unrun.
    assert policies == {}
    assert problems[0].startswith("agent ops-bot: [permissive-default]")
