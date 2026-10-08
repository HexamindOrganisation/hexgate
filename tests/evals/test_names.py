"""Caller attributes no audit row sends (`names.py`)."""

from __future__ import annotations

import pytest

from evals.policy_writing.names import unknown_attrs
from hexgate.security.policy_set import load_policy_set_from_dict
from tests.evals.helpers import ATTRS


def loaded(doc: dict):
    return load_policy_set_from_dict({"version": 1, **doc})


def on(tool: str, *constraints: str) -> dict:
    return {"tools": {tool: {"mode": "allow", "constraints": list(constraints)}}}


def test_unknown_attrs_happy_path() -> None:
    doc = on("refund_order", 'ctx.department == "billing"', "args.amount < 5")
    assert unknown_attrs(loaded(doc), ATTRS) == []


def test_when_an_attribute_is_in_no_audit_row_then_unknown_attrs_flags_it() -> None:
    doc = on("refund_order", 'ctx.tier == "x"')
    assert unknown_attrs(loaded(doc), ATTRS) == ["refund_order: ctx.tier"]


def test_when_a_name_is_inside_a_string_then_unknown_attrs_ignores_it() -> None:
    # `ctx.x` inside a string is data, and quantifier bodies are walked.
    constraint = 'args.note == "see ctx.x" and any(args.items, .price < 5)'
    assert unknown_attrs(loaded(on("refund_order", constraint)), ATTRS) == []


@pytest.mark.parametrize(
    "doc",
    [
        {"constraints": ['ctx.departmnt == "x"']},  # a flat file's own fence
        {"constraints": ['ctx.departmnt == "x"'], "roles": {"billing": {}}},
        {"roles": {"billing": {"constraints": ['ctx.departmnt == "x"']}}},
    ],
)
def test_when_a_file_role_or_default_constraint_has_a_typo_then_unknown_attrs_flags_it(
    doc,
) -> None:
    assert unknown_attrs(loaded(doc), ATTRS) == ["policy-level: ctx.departmnt"]


def test_when_a_skill_or_gate_constraint_reads_an_invented_attribute_then_unknown_attrs_flags_it() -> (
    None
):
    doc = {
        "skills": {"pdf": {"mode": "allow", "constraints": ["ctx.invented == 1"]}},
        "agents": {"ops-bot": {"mode": "allow", "constraints": ["ctx.invented == 1"]}},
    }
    refs = unknown_attrs(loaded(doc), ATTRS)
    assert "skill:pdf: ctx.invented" in refs
    assert any(r.startswith("agent.") and r.endswith(": ctx.invented") for r in refs)


@pytest.mark.parametrize(
    "doc",
    [
        {"tools": {"refund_order": {"mode": "deny", "constraints": ["ctx.tier == 1"]}}},
        {"default_policy": {"mode": "deny", "constraints": ["ctx.tier == 1"]}},
    ],
)
def test_when_a_deny_reads_an_attribute_then_unknown_attrs_ignores_it(doc) -> None:
    # A deny's constraints never run, as the SDK's checks treat them.
    assert unknown_attrs(loaded(doc), ATTRS) == []


def test_when_a_default_policy_constraint_has_a_typo_then_unknown_attrs_names_it() -> (
    None
):
    doc = {"default_policy": {"mode": "allow", "constraints": ['ctx.departmnt == "x"']}}
    assert unknown_attrs(loaded(doc), ATTRS) == ["default_policy: ctx.departmnt"]
