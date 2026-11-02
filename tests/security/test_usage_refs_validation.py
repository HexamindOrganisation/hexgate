"""Load-time validation of ``agent_usage.*`` and ``user_usage.*`` references, and the
referenced-path sets.

Before this check, a usage typo loaded cleanly and resolved to missing at runtime,
denying every call. The referenced-path set is the enforcer's signal to enable the
ledgers, so a path missing from it would fail its constraint closed.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from hexgate.runtime.agent_usage import KNOWN_AGENT_USAGE_PATHS, KNOWN_USER_USAGE_PATHS
from hexgate.security.models import AgentPolicy
from hexgate.security.policy_set import (
    DEFAULT_ROLE_NAME,
    PolicySet,
    PolicySetError,
    load_policy_set_from_dict,
)
from hexgate.security.usage_inputs import AGENT_USAGE_ROOT, USER_USAGE_ROOT

_VOCABULARY = "<metric>_<window>"
_WINDOWS = "windows: 5m, 1h, 24h, 7d, 30d"
_ROOTS = (AGENT_USAGE_ROOT, USER_USAGE_ROOT)
_KNOWN_PATHS = {
    AGENT_USAGE_ROOT: KNOWN_AGENT_USAGE_PATHS,
    USER_USAGE_ROOT: KNOWN_USER_USAGE_PATHS,
}
_REFERENCED_PATHS: dict[str, Callable[[PolicySet], frozenset[str]]] = {
    AGENT_USAGE_ROOT: PolicySet.agent_usage_paths,
    USER_USAGE_ROOT: PolicySet.user_usage_paths,
}
_SURFACES: dict[str, Callable[[str], dict]] = {
    "policy-level": lambda constraint: {"constraints": [constraint]},
    "default-policy": lambda constraint: {
        "default_policy": {"mode": "allow", "constraints": [constraint]}
    },
    "admission": lambda constraint: {
        "admission": {"mode": "allow", "constraints": [constraint]}
    },
    "reach": lambda constraint: {
        "agents": {
            "other": {"via": ["handoff"], "mode": "allow", "constraints": [constraint]}
        }
    },
}


def _unknown(root: str) -> str:
    return f"unknown {root}.* path"


def _tool_policy(*constraints: str) -> AgentPolicy:
    return AgentPolicy.model_validate(
        {"tools": {"refund": {"mode": "allow", "constraints": list(constraints)}}}
    )


def _policy_set(policy: AgentPolicy) -> PolicySet:
    return PolicySet({DEFAULT_ROLE_NAME: policy})


@pytest.mark.parametrize(
    ("root", "path"),
    [(root, path) for root in _ROOTS for path in sorted(_KNOWN_PATHS[root])],
)
def test_every_registered_path_loads(root: str, path: str) -> None:
    _policy_set(_tool_policy(f"{root}.{path} < 5"))


@pytest.mark.parametrize("root", _ROOTS)
@pytest.mark.parametrize(
    "path",
    ["tool_call_1h", "tool_calls_2h", "tool_calls"],
    ids=["metric-typo", "unknown-window", "no-window"],
)
def test_an_unregistered_path_is_rejected_with_the_rule(root: str, path: str) -> None:
    with pytest.raises(PolicySetError) as exc:
        _policy_set(_tool_policy(f"{root}.{path} < 5"))

    message = str(exc.value)
    assert f"{_unknown(root)} {path!r}" in message
    assert _VOCABULARY in message
    assert _WINDOWS in message


@pytest.mark.parametrize("root", _ROOTS)
def test_a_too_deep_path_is_rejected(root: str) -> None:
    with pytest.raises(PolicySetError, match="exactly two segments"):
        _policy_set(_tool_policy(f"{root}.tool_calls_1h.x < 5"))


@pytest.mark.parametrize("root", _ROOTS)
def test_counting_the_whole_root_is_rejected(root: str) -> None:
    """The engines always see a usage map, so ``count()`` over it would read an
    anonymous caller's empty map as a real zero."""
    with pytest.raises(PolicySetError, match=root):
        _policy_set(_tool_policy(f"count({root}) > 0"))


@pytest.mark.parametrize("root", _ROOTS)
@pytest.mark.parametrize("surface", _SURFACES)
def test_every_constraint_surface_is_linted(root: str, surface: str) -> None:
    document = _SURFACES[surface](f"{root}.tool_call_1h < 5")

    with pytest.raises(PolicySetError, match=_unknown(root)):
        _policy_set(AgentPolicy.model_validate(document))


@pytest.mark.parametrize("root", _ROOTS)
def test_the_platform_loader_rejects_a_typo_too(root: str) -> None:
    with pytest.raises(PolicySetError, match=_unknown(root)):
        load_policy_set_from_dict(
            {
                "tools": {
                    "refund": {
                        "mode": "allow",
                        "constraints": [f"{root}.invocation_1h < 5"],
                    }
                }
            }
        )


@pytest.mark.parametrize("root", _ROOTS)
def test_the_referenced_paths_union_every_role_and_surface(root: str) -> None:
    policy_set = load_policy_set_from_dict(
        {
            "roles": {
                "default": {
                    "constraints": [f"{root}.denials_5m < 5"],
                    "admission": {
                        "mode": "allow",
                        "constraints": [f"{root}.invocations_1h < 100"],
                    },
                },
                "billing": {
                    "tools": {
                        "refund": {
                            "mode": "allow",
                            "constraints": [
                                f"{root}.total_tokens_24h <= 1000",
                                "run.tool_calls < 5",
                            ],
                        }
                    }
                },
            }
        }
    )

    assert _REFERENCED_PATHS[root](policy_set) == {
        "denials_5m",
        "invocations_1h",
        "total_tokens_24h",
    }


@pytest.mark.parametrize("root", _ROOTS)
def test_a_usage_free_policy_references_no_path(root: str) -> None:
    policy_set = _policy_set(_tool_policy("run.tool_calls < 5", "args.amount < 10"))

    assert _REFERENCED_PATHS[root](policy_set) == frozenset()


def test_the_two_roots_are_kept_apart() -> None:
    policy_set = _policy_set(
        _tool_policy(
            f"{AGENT_USAGE_ROOT}.invocations_1h < 1000",
            f"{USER_USAGE_ROOT}.denials_5m < 5",
        )
    )

    assert policy_set.agent_usage_paths() == {"invocations_1h"}
    assert policy_set.user_usage_paths() == {"denials_5m"}


def test_an_agent_only_policy_references_no_user_path() -> None:
    policy_set = _policy_set(_tool_policy(f"{AGENT_USAGE_ROOT}.invocations_1h < 5"))

    assert policy_set.user_usage_paths() == frozenset()
