"""Load-time validation of ``user_usage.*`` references, and the referenced-path set.

The root becomes loadable here, with the values that feed it: a typo is rejected at
load, as for ``agent_usage.*``, instead of denying every call at runtime.
"""

from __future__ import annotations

import pytest

from hexgate.runtime.agent_usage import KNOWN_USER_USAGE_PATHS
from hexgate.security.models import AgentPolicy
from hexgate.security.policy_set import (
    DEFAULT_ROLE_NAME,
    PolicySet,
    PolicySetError,
    load_policy_set_from_dict,
)

_UNKNOWN = "unknown user_usage.* path"
_VOCABULARY = "<metric>_<window>"


def _tool_policy(*constraints: str) -> AgentPolicy:
    return AgentPolicy.model_validate(
        {"tools": {"refund": {"mode": "allow", "constraints": list(constraints)}}}
    )


def _policy_set(policy: AgentPolicy) -> PolicySet:
    return PolicySet({DEFAULT_ROLE_NAME: policy})


@pytest.mark.parametrize("path", sorted(KNOWN_USER_USAGE_PATHS))
def test_every_registered_path_loads(path: str) -> None:
    _policy_set(_tool_policy(f"user_usage.{path} < 5"))


@pytest.mark.parametrize(
    "path",
    ["tool_call_1h", "tool_calls_2h", "invocations"],
    ids=["metric-typo", "unknown-window", "no-window"],
)
def test_an_unregistered_path_is_rejected_with_the_rule(path: str) -> None:
    with pytest.raises(PolicySetError) as exc:
        _policy_set(_tool_policy(f"user_usage.{path} < 5"))

    message = str(exc.value)
    assert f"{_UNKNOWN} {path!r}" in message
    assert _VOCABULARY in message


def test_a_too_deep_path_is_rejected() -> None:
    with pytest.raises(PolicySetError, match="exactly two segments"):
        _policy_set(_tool_policy("user_usage.invocations_1h.x < 5"))


def test_counting_the_whole_root_is_rejected() -> None:
    """The engines always see a ``user_usage`` map, so ``count()`` over it would
    read an anonymous caller's empty map as a real zero."""
    with pytest.raises(PolicySetError, match="user_usage"):
        _policy_set(_tool_policy("count(user_usage) > 0"))


@pytest.mark.parametrize(
    "document",
    [
        {"constraints": ["user_usage.tool_call_1h < 5"]},
        {
            "default_policy": {
                "mode": "allow",
                "constraints": ["user_usage.tool_call_1h < 5"],
            }
        },
        {
            "admission": {
                "mode": "allow",
                "constraints": ["user_usage.tool_call_1h < 5"],
            }
        },
        {
            "agents": {
                "other": {
                    "via": ["handoff"],
                    "mode": "allow",
                    "constraints": ["user_usage.tool_call_1h < 5"],
                }
            }
        },
    ],
    ids=["policy-level", "default-policy", "admission", "reach"],
)
def test_every_constraint_surface_is_linted(document: dict) -> None:
    with pytest.raises(PolicySetError, match=_UNKNOWN):
        _policy_set(AgentPolicy.model_validate(document))


def test_the_referenced_paths_union_every_role_and_surface() -> None:
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {
                    "constraints": ["user_usage.denials_5m < 5"],
                    "admission": {
                        "mode": "allow",
                        "constraints": ["user_usage.invocations_1h < 50"],
                    },
                },
                "billing": {
                    "tools": {
                        "refund": {
                            "mode": "allow",
                            "constraints": ["user_usage.total_tokens_24h <= 1000"],
                        }
                    }
                },
            }
        }
    )

    assert policy_set.user_usage_paths() == {
        "denials_5m",
        "invocations_1h",
        "total_tokens_24h",
    }


def test_the_two_roots_are_kept_apart() -> None:
    policy_set = _policy_set(
        _tool_policy("agent_usage.invocations_1h < 1000", "user_usage.denials_5m < 5")
    )

    assert policy_set.agent_usage_paths() == {"invocations_1h"}
    assert policy_set.user_usage_paths() == {"denials_5m"}


def test_an_agent_only_policy_references_no_user_path() -> None:
    policy_set = _policy_set(_tool_policy("agent_usage.invocations_1h < 5"))

    assert policy_set.user_usage_paths() == frozenset()
