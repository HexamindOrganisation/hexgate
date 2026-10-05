"""Load-time validation of ``agent_usage.*`` references, and the referenced-path set.

Before this check, an ``agent_usage.*`` typo loaded cleanly and resolved to missing at
runtime, denying every call. The referenced-path set is the enforcer's signal to
enable the ledgers, so a path missing from it would fail its constraint closed.
"""

from __future__ import annotations

import pytest

from hexgate.runtime.agent_usage import KNOWN_AGENT_USAGE_PATHS
from hexgate.security.models import AgentPolicy
from hexgate.security.policy_set import (
    DEFAULT_ROLE_NAME,
    PolicySet,
    PolicySetError,
    load_policy_set_from_dict,
)

_UNKNOWN = "unknown agent_usage.* path"
_VOCABULARY = "<metric>_<window>"


def _tool_policy(*constraints: str) -> AgentPolicy:
    return AgentPolicy.model_validate(
        {"tools": {"refund": {"mode": "allow", "constraints": list(constraints)}}}
    )


def _policy_set(policy: AgentPolicy) -> PolicySet:
    return PolicySet({DEFAULT_ROLE_NAME: policy})


@pytest.mark.parametrize("path", sorted(KNOWN_AGENT_USAGE_PATHS))
def test_every_registered_path_loads(path: str) -> None:
    _policy_set(_tool_policy(f"agent_usage.{path} < 5"))


@pytest.mark.parametrize(
    "path",
    ["tool_call_1h", "tool_calls_2h", "tool_calls"],
    ids=["metric-typo", "unknown-window", "no-window"],
)
def test_an_unregistered_path_is_rejected_with_the_rule(path: str) -> None:
    with pytest.raises(PolicySetError) as exc:
        _policy_set(_tool_policy(f"agent_usage.{path} < 5"))

    message = str(exc.value)
    assert f"{_UNKNOWN} {path!r}" in message
    assert _VOCABULARY in message
    assert "windows: 5m, 1h, 24h, 7d, 30d" in message


def test_a_too_deep_path_is_rejected() -> None:
    with pytest.raises(PolicySetError, match="exactly two segments"):
        _policy_set(_tool_policy("agent_usage.tool_calls_1h.x < 5"))


@pytest.mark.parametrize(
    "document",
    [
        {"constraints": ["agent_usage.tool_call_1h < 5"]},
        {
            "default_policy": {
                "mode": "allow",
                "constraints": ["agent_usage.tool_call_1h < 5"],
            }
        },
        {
            "admission": {
                "mode": "allow",
                "constraints": ["agent_usage.tool_call_1h < 5"],
            }
        },
        {
            "agents": {
                "other": {
                    "via": ["handoff"],
                    "mode": "allow",
                    "constraints": ["agent_usage.tool_call_1h < 5"],
                }
            }
        },
    ],
    ids=["policy-level", "default-policy", "admission", "reach"],
)
def test_every_constraint_surface_is_linted(document: dict) -> None:
    with pytest.raises(PolicySetError, match=_UNKNOWN):
        _policy_set(AgentPolicy.model_validate(document))


def test_the_platform_loader_rejects_a_typo_too() -> None:
    with pytest.raises(PolicySetError, match=_UNKNOWN):
        load_policy_set_from_dict(
            {
                "tools": {
                    "refund": {
                        "mode": "allow",
                        "constraints": ["agent_usage.invocation_1h < 5"],
                    }
                }
            }
        )


def test_the_referenced_paths_union_every_role_and_surface() -> None:
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {
                    "constraints": ["agent_usage.denials_5m < 5"],
                    "admission": {
                        "mode": "allow",
                        "constraints": ["agent_usage.invocations_1h < 100"],
                    },
                },
                "billing": {
                    "tools": {
                        "refund": {
                            "mode": "allow",
                            "constraints": [
                                "agent_usage.total_tokens_24h <= 1000",
                                "run.tool_calls < 5",
                            ],
                        }
                    }
                },
            }
        }
    )

    assert policy_set.agent_usage_paths() == {
        "denials_5m",
        "invocations_1h",
        "total_tokens_24h",
    }


def test_a_usage_free_policy_references_no_path() -> None:
    policy_set = _policy_set(_tool_policy("run.tool_calls < 5", "args.amount < 10"))

    assert policy_set.agent_usage_paths() == frozenset()
