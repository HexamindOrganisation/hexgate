"""Risk facts: the pure reading of one role's policy, then the route over a
composed project with registered manifests."""

from __future__ import annotations

import pytest
from hexgate.manifest.models import AgentManifest
from hexgate.security import AgentPolicy

import hexgate_api.features.agents.service as asvc
from hexgate_api.features.policy_modules.risk_facts import facts_for
from tests.features.policy_modules.test_policy_modules import (
    _dummy_sign,
    _project,
    _put_file,
    client,  # noqa: F401 — the fixture, used by name
)


def _manifest() -> AgentManifest:
    def tool(tool_name: str, **props: str) -> dict:
        properties = {p: {"title": p, "type": t} for p, t in props.items()}
        schema = {"properties": properties, "required": []}
        return {"name": tool_name, "description": "", "input_schema": schema}

    return AgentManifest.model_validate(
        {
            "name": "bot",
            "framework": "hexgate",
            "tools": [
                tool("refund_order", amount="number", currency="string"),
                tool("delete_user", user_id="string"),
            ],
        }
    )


def _facts(
    policy: dict, manifest: AgentManifest | None = None, role: str = "support"
) -> set[tuple]:
    roles = {"default", "support", "admin"}
    found = facts_for("bot", role, AgentPolicy.model_validate(policy), manifest, roles)
    return {(f["tool"], f["kind"], f["argument"]) for f in found}


def _refund(*constraints: str, mode: str = "allow") -> dict:
    return {"tools": {"refund_order": {"mode": mode, "constraints": list(constraints)}}}


UNBOUNDED = ("refund_order", "unbounded_number", "amount")


def test_facts_for_happy_path():
    """The amount is still open when the only constraint pins the currency."""
    policy = AgentPolicy.model_validate(_refund('args.currency == "EUR"'))
    assert facts_for("bot", "support", policy, _manifest(), {"support"}) == [
        {
            "agent": "bot",
            "role": "support",
            "tool": "refund_order",
            "kind": "unbounded_number",
            "argument": "amount",
            "constraints_present": ['args.currency == "EUR"'],
        }
    ]


@pytest.mark.parametrize(
    "policy",
    [
        _refund("args.amount <= 500"),
        _refund("500 >= args.amount"),
        _refund("args.amount in [10, 20]"),
        {**_refund("args.amount <= consts.max"), "consts": {"max": 500}},
        _refund("500 == args.amount"),
        _refund('args.amount <= 100 or (args.currency == "EUR" and args.amount < 50)'),
        _refund('args.currency == "EUR" and args.amount <= 500'),
        {**_refund(), "constraints": ["args.amount <= 100"]},
        {**_refund(), "constraints": ['tool != "refund_order" or args.amount <= 9']},
        {
            **_refund(),
            "constraints": ['startswith(tool, "agent.") or args.amount <= 9'],
        },
        _refund('role == "admin" and args.amount >= 0'),
    ],
)
def test_when_amount_is_capped_then_no_fact(policy):
    assert UNBOUNDED not in _facts(policy, _manifest())


@pytest.mark.parametrize(
    "constraint",
    [
        "args.amount >= 1",
        "args.amount <= args.limit",
        "not (args.amount < 5)",
        # A mistyped amount fails the inner comparison, so the negation passes it.
        "not (args.amount > 500)",
        'args.amount <= 100 or args.currency == "EUR"',
        "any(args.items, . <= 5)",
        # Strings compare as text, so "1000000" passes.
        'args.amount <= "500"',
        "args.amount <= consts.text_max",
        "args.amount <= ctx.limit",
        'role == "support" or args.amount <= 100',
    ],
)
def test_when_amount_has_no_upper_bound_then_it_is_a_fact(constraint):
    policy = {**_refund(constraint), "consts": {"text_max": "500"}}
    assert UNBOUNDED in _facts(policy, _manifest())


@pytest.mark.parametrize(
    ("constraint", "flagged"),
    [
        # The default bucket serves every undefined role, so "contractor" reaches it.
        ('role == "contractor" or args.amount <= 9', True),
        ('startswith(role, "adm") or args.amount <= 9', True),
        # ...but no caller holding "admin" does.
        ('args.amount <= 9 or role == "admin"', False),
        ('role in ["admin", "support"] or args.amount <= 9', False),
    ],
)
def test_when_the_role_is_the_default_bucket_then_only_named_roles_are_decided(
    constraint, flagged
):
    policy = {**_refund(), "constraints": [constraint]}
    assert (UNBOUNDED in _facts(policy, _manifest(), role="default")) is flagged


@pytest.mark.parametrize("mode", ["approval_required", "deny"])
def test_when_the_call_is_not_allowed_outright_then_no_fact(mode):
    assert _facts(_refund(mode=mode), _manifest()) == set()


def test_when_a_tool_falls_to_an_allowing_default_then_it_counts():
    facts = _facts({"default_policy": {"mode": "allow"}}, _manifest())
    assert facts == {
        UNBOUNDED,
        ("delete_user", "destructive_without_approval", None),
    }


@pytest.mark.parametrize(
    ("tool", "destructive"),
    [("delete_user", True), ("deleteUser", True), ("db.drop_table", True),
     ("HTTPDelete", True),
     ("undelete_user", False), ("view_orders", False)],
)  # fmt: skip
def test_when_a_word_of_the_name_is_destructive_then_it_is_a_fact(tool, destructive):
    fact = (tool, "destructive_without_approval", None)
    assert (fact in _facts({"tools": {tool: {"mode": "allow"}}})) is destructive


def test_when_there_is_no_manifest_then_arguments_raise_no_fact():
    assert _facts(_refund()) == set()


# --- the route --------------------------------------------------------------

ENTRY = (
    "agents:\n"
    "  bot:\n"
    "    roles:\n"
    "      support:\n"
    '        tools: { refund_order: { mode: allow, constraints: ["args.currency == \\"EUR\\""] } }\n'
    "  helper:\n"
    "    roles:\n"
    "      support:\n"
    "        tools: { delete_user: { mode: allow } }\n"
)


async def _register(session_factory, project_id: str, manifest: AgentManifest):
    from hexgate_api.schemas import AgentManifest as WireManifest

    async with session_factory() as s:
        wire = WireManifest.model_validate(manifest.model_dump())
        await asvc.register_manifest(s, project_id, wire, sign=_dummy_sign)


async def test_api_policy_risk_facts_happy_path(
    client,  # noqa: F811 — the imported fixture
    session_factory,
):
    pid = _project(client)
    assert _put_file(client, pid, "policy.yaml", ENTRY).status_code == 200
    await _register(session_factory, pid, _manifest())
    async with session_factory() as s:  # registered, but no manifest loads
        await asvc._get_or_create_agent(s, pid, "ops")
        await s.commit()

    body = client.get(f"/v1/projects/{pid}/policy/risk-facts").json()
    found = {(f["agent"], f["tool"], f["kind"], f["argument"]) for f in body["facts"]}
    assert found == {
        ("bot", "refund_order", "unbounded_number", "amount"),
        ("helper", "delete_user", "destructive_without_approval", None),
    }
    assert body["agents_without_manifest"] == ["helper", "ops"]

    one = client.get(f"/v1/projects/{pid}/policy/risk-facts", params={"agent": "bot"})
    assert {f["agent"] for f in one.json()["facts"]} == {"bot"}
    assert one.json()["agents_without_manifest"] == []
    typo = client.get(f"/v1/projects/{pid}/policy/risk-facts", params={"agent": "bott"})
    assert typo.status_code == 404


def test_when_no_agent_is_named_then_the_generic_view_answers(client):  # noqa: F811
    pid = _project(client)
    entry = "tools: { purge_cache: { mode: allow } }\n"
    assert _put_file(client, pid, "policy.yaml", entry).status_code == 200

    body = client.get(f"/v1/projects/{pid}/policy/risk-facts").json()
    assert [(f["agent"], f["tool"]) for f in body["facts"]] == [("*", "purge_cache")]
    assert body["agents_without_manifest"] == ["*"]
