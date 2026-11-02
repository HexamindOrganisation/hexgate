"""The ``UsageInputs`` decision input: its contract, and the structural parity
that both engines build their usage roots from ``namespaces()``."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from hexgate.security.constraints import PATH_ROOTS
from hexgate.security.usage_inputs import NO_USAGE, UsageInputs
from hexgate.security.wasm_engine import WasmPolicy


def test_no_usage_exposes_both_roots_empty() -> None:
    assert NO_USAGE.namespaces() == {"agent_usage": {}, "user_usage": {}}


def test_namespaces_copies_the_callers_mapping() -> None:
    source = {"invocations_1h": 3}

    namespaces = UsageInputs(agent_usage=source).namespaces()
    namespaces["agent_usage"]["invocations_1h"] = 99

    assert source == {"invocations_1h": 3}
    assert UsageInputs(agent_usage=source).namespaces()["agent_usage"] == source


def test_user_usage_is_exposed_under_its_root() -> None:
    namespaces = UsageInputs(user_usage={"x": 1}).namespaces()

    assert namespaces["user_usage"] == {"x": 1}
    assert namespaces["agent_usage"] == {}


def test_usage_inputs_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        NO_USAGE.agent_usage = {}  # type: ignore[misc]


def test_every_exposed_root_is_a_lint_root() -> None:
    assert set(NO_USAGE.namespaces()) <= set(PATH_ROOTS)


def test_wasm_input_carries_exactly_the_usage_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        WasmPolicy, "evaluate", lambda _self, input_obj: captured.append(input_obj)
    )
    policy = object.__new__(WasmPolicy)

    policy.decide(
        role="default",
        tool="t",
        args={},
        usage=UsageInputs(
            agent_usage={"invocations_1h": 1}, user_usage={"invocations_1h": 2}
        ),
    )

    (input_obj,) = captured
    assert set(input_obj) == {
        "role",
        "tool",
        "args",
        "ctx",
        "run",
        *NO_USAGE.namespaces(),
    }
    assert input_obj["agent_usage"] == {"invocations_1h": 1}
    assert input_obj["user_usage"] == {"invocations_1h": 2}
