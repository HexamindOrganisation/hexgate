"""Unit tests for the Biscuit-fact-driven policy predicates."""

from __future__ import annotations

import pytest

from fortify.security import PolicyDeniedError
from fortify.security.predicates import (
    check_numeric_limit,
    check_requires_scope,
    check_requires_user,
)


# ---------------------------------------------------------------------------
# check_requires_user
# ---------------------------------------------------------------------------


def test_requires_user_passes_when_user_in_allowed_list() -> None:
    """A user fact that overlaps the allowed list is fine."""
    check_requires_user(
        allowed=["alice", "carol"],
        facts={"user": ["alice"]},
        tool_name="refund",
    )  # no exception


def test_requires_user_denies_when_user_not_in_allowed_list() -> None:
    """A user fact outside the allowed list is denied with a clear message."""
    with pytest.raises(PolicyDeniedError, match=r"requires user in \['alice', 'carol'\]"):
        check_requires_user(
            allowed=["alice", "carol"],
            facts={"user": ["bob"]},
            tool_name="refund",
        )


def test_requires_user_denies_when_no_user_fact() -> None:
    """A requirement plus an empty fact set denies — fail-closed."""
    with pytest.raises(PolicyDeniedError, match="token carries none"):
        check_requires_user(
            allowed=["alice"],
            facts={"scope": ["read"]},  # no user fact at all
            tool_name="refund",
        )


def test_requires_user_skipped_when_allowed_is_none() -> None:
    """No requirement → predicate is a no-op even with empty facts."""
    check_requires_user(allowed=None, facts=None, tool_name="refund")


def test_requires_user_skipped_when_allowed_is_empty() -> None:
    """Empty allowed list reads as 'no requirement', matching the None default."""
    check_requires_user(allowed=[], facts={"user": ["bob"]}, tool_name="refund")


def test_requires_user_handles_multiple_user_facts() -> None:
    """A token attenuated with multiple user facts passes if any matches."""
    check_requires_user(
        allowed=["alice"],
        facts={"user": ["bob", "alice"]},  # both present from different blocks
        tool_name="refund",
    )


# ---------------------------------------------------------------------------
# check_requires_scope
# ---------------------------------------------------------------------------


def test_requires_scope_passes_when_all_scopes_present() -> None:
    """All required scopes must be in the token's scope facts."""
    check_requires_scope(
        required=["refund", "read"],
        facts={"scope": ["read", "refund", "write"]},
        tool_name="refund",
    )


def test_requires_scope_denies_with_missing_scope() -> None:
    """A single missing scope denies with the list of misses."""
    with pytest.raises(PolicyDeniedError, match=r"requires scopes \['refund'\]"):
        check_requires_scope(
            required=["read", "refund"],
            facts={"scope": ["read"]},
            tool_name="refund",
        )


def test_requires_scope_skipped_when_required_is_none() -> None:
    """No requirement → no-op."""
    check_requires_scope(required=None, facts=None, tool_name="refund")


def test_requires_scope_denies_when_facts_dict_missing_scope_key() -> None:
    """A token with no scope fact at all denies when scopes are required."""
    with pytest.raises(PolicyDeniedError):
        check_requires_scope(
            required=["read"],
            facts={"user": ["alice"]},
            tool_name="refund",
        )


# ---------------------------------------------------------------------------
# check_numeric_limit
# ---------------------------------------------------------------------------


def test_numeric_limit_passes_when_under_cap() -> None:
    """Arg below the fact-supplied bound is allowed."""
    check_numeric_limit(
        limits={"amount": "refund_limit"},
        facts={"refund_limit": [50]},
        arguments={"amount": 30},
        tool_name="refund",
    )


def test_numeric_limit_passes_at_exact_cap() -> None:
    """The cap is inclusive (``<=``)."""
    check_numeric_limit(
        limits={"amount": "refund_limit"},
        facts={"refund_limit": [50]},
        arguments={"amount": 50},
        tool_name="refund",
    )


def test_numeric_limit_denies_above_cap() -> None:
    """Above-cap calls deny with the requested-vs-limit message."""
    with pytest.raises(PolicyDeniedError, match=r"caps argument \"amount\" at 50"):
        check_numeric_limit(
            limits={"amount": "refund_limit"},
            facts={"refund_limit": [50]},
            arguments={"amount": 200},
            tool_name="refund",
        )


def test_numeric_limit_uses_most_restrictive_when_multiple_facts() -> None:
    """Multiple attenuators each narrowing the cap → most restrictive wins."""
    with pytest.raises(PolicyDeniedError, match="at 20"):
        check_numeric_limit(
            limits={"amount": "refund_limit"},
            facts={"refund_limit": [50, 20, 100]},  # min(50, 20, 100) = 20
            arguments={"amount": 30},
            tool_name="refund",
        )


def test_numeric_limit_denies_when_fact_missing() -> None:
    """A policy that caps via a fact name not present in the token denies."""
    with pytest.raises(PolicyDeniedError, match="token carries no such fact"):
        check_numeric_limit(
            limits={"amount": "refund_limit"},
            facts={"user": ["alice"]},
            arguments={"amount": 5},
            tool_name="refund",
        )


def test_numeric_limit_skipped_when_arg_not_supplied() -> None:
    """The cap is on an argument the call didn't include → not our concern."""
    check_numeric_limit(
        limits={"amount": "refund_limit"},
        facts={"refund_limit": [50]},
        arguments={},  # no amount kwarg
        tool_name="refund",
    )


def test_numeric_limit_denies_non_numeric_argument() -> None:
    """A mapped argument that isn't int/float fails loudly."""
    with pytest.raises(PolicyDeniedError, match="passed str"):
        check_numeric_limit(
            limits={"amount": "refund_limit"},
            facts={"refund_limit": [50]},
            arguments={"amount": "lots"},
            tool_name="refund",
        )


def test_numeric_limit_skipped_when_limits_is_none() -> None:
    """No limit configured → no-op."""
    check_numeric_limit(
        limits=None,
        facts=None,
        arguments={"amount": 99999},
        tool_name="refund",
    )
