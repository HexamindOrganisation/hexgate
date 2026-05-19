"""Tests for the M1 constraint grammar — parser, evaluator, end-to-end check."""

from __future__ import annotations

import pytest

from fortify.security import PolicyDeniedError
from fortify.security.constraints import (
    ConstraintParseError,
    check_constraints,
    evaluate_constraint,
    parse_constraint,
)


# ---------------------------------------------------------------------------
# parse_constraint — happy paths
# ---------------------------------------------------------------------------


def test_parse_numeric_comparison() -> None:
    """``args.amount <= 50`` parses to a Constraint with path + op + literal."""
    c = parse_constraint("args.amount <= 50")
    assert c.path == ("args", "amount")
    assert c.op == "<="
    assert c.value == 50


def test_parse_string_equality() -> None:
    """JSON double-quoted strings on the right."""
    c = parse_constraint('args.currency == "USD"')
    assert c.op == "=="
    assert c.value == "USD"


def test_parse_in_list() -> None:
    """The ``in`` operator requires a JSON list on the right."""
    c = parse_constraint('args.template in ["a", "b", "c"]')
    assert c.op == "in"
    assert c.value == ["a", "b", "c"]


def test_parse_not_in_list() -> None:
    """The ``not in`` operator is treated as a single two-word operator."""
    c = parse_constraint('args.priority not in ["urgent"]')
    assert c.op == "not in"


def test_parse_boolean_rhs() -> None:
    """``true`` / ``false`` are JSON literals — supported by the RHS parser."""
    c = parse_constraint("args.confirmed == true")
    assert c.value is True


def test_parse_deep_path() -> None:
    """Multi-segment paths walk into nested dicts at evaluation time."""
    c = parse_constraint("args.payment.amount <= 100")
    assert c.path == ("args", "payment", "amount")


# ---------------------------------------------------------------------------
# parse_constraint — error paths
# ---------------------------------------------------------------------------


def test_parse_rejects_empty_source() -> None:
    with pytest.raises(ConstraintParseError, match="empty"):
        parse_constraint("")


def test_parse_rejects_unknown_operator() -> None:
    with pytest.raises(ConstraintParseError, match="no recognised operator"):
        parse_constraint("args.amount ~~ 50")


def test_parse_rejects_missing_rhs() -> None:
    with pytest.raises(ConstraintParseError, match="missing right-hand side"):
        parse_constraint("args.amount <= ")


def test_parse_rejects_single_quoted_rhs() -> None:
    """JSON only accepts double quotes — single quotes are a clear error."""
    with pytest.raises(ConstraintParseError, match="JSON literal"):
        parse_constraint("args.x == 'single'")


def test_parse_rejects_invalid_identifier() -> None:
    with pytest.raises(ConstraintParseError, match="invalid identifier"):
        parse_constraint("args.0bad <= 50")


def test_parse_in_requires_list_rhs() -> None:
    with pytest.raises(ConstraintParseError, match="requires a list"):
        parse_constraint('args.template in "string-not-list"')


# ---------------------------------------------------------------------------
# evaluate_constraint — semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("src", "args", "expected"),
    [
        ("args.amount <= 50", {"amount": 30}, True),
        ("args.amount <= 50", {"amount": 50}, True),
        ("args.amount <= 50", {"amount": 51}, False),
        ("args.amount < 50", {"amount": 50}, False),
        ('args.currency == "USD"', {"currency": "USD"}, True),
        ('args.currency == "USD"', {"currency": "EUR"}, False),
        ('args.currency != "USD"', {"currency": "EUR"}, True),
        ('args.template in ["a", "b"]', {"template": "a"}, True),
        ('args.template in ["a", "b"]', {"template": "c"}, False),
        ('args.priority not in ["urgent"]', {"priority": "low"}, True),
        ('args.priority not in ["urgent"]', {"priority": "urgent"}, False),
    ],
)
def test_evaluate_truth_table(src: str, args: dict, expected: bool) -> None:
    """Spot-check the operator semantics across types."""
    c = parse_constraint(src)
    assert evaluate_constraint(c, {"args": args}) is expected


def test_evaluate_missing_path_fails_closed() -> None:
    """A constraint over an argument the call didn't supply denies."""
    c = parse_constraint("args.amount <= 50")
    assert evaluate_constraint(c, {"args": {}}) is False


def test_evaluate_type_mismatch_fails_closed() -> None:
    """Comparing a str argument to an int RHS never raises — fails closed."""
    c = parse_constraint("args.amount <= 50")
    assert evaluate_constraint(c, {"args": {"amount": "fifty"}}) is False


# ---------------------------------------------------------------------------
# check_constraints — caller surface
# ---------------------------------------------------------------------------


def test_check_constraints_empty_is_noop() -> None:
    """No constraints → no raise even with empty arguments."""
    check_constraints([], None, "any_tool")


def test_check_constraints_passes_when_all_satisfied() -> None:
    """All constraints satisfied → silent return."""
    check_constraints(
        ["args.amount <= 50", 'args.currency == "USD"'],
        {"amount": 30, "currency": "USD"},
        "refund",
    )


def test_check_constraints_raises_on_first_failure() -> None:
    """The first failing constraint raises; remaining constraints aren't evaluated."""
    with pytest.raises(PolicyDeniedError, match="args.amount <= 50"):
        check_constraints(
            ["args.amount <= 50", 'args.currency == "USD"'],
            {"amount": 999, "currency": "USD"},
            "refund",
        )


def test_check_constraints_accepts_pre_parsed_constraint_objects() -> None:
    """Pre-parsed Constraint objects work alongside raw strings."""
    parsed = parse_constraint("args.amount <= 50")
    check_constraints([parsed], {"amount": 10}, "refund")
