"""The assistant verifies the token this API mints for it with a ``client_id``
and an ``aud`` copied into its own package, which never imports the API. Each
test reads the assistant's source by ``ast`` and fails when a copy drifts."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hexgate_api.constants import ASSISTANT_CLIENT_ID
from hexgate_api.settings import Settings

ASSISTANT = Path(__file__).parents[4] / "assistant" / "hexgate_assistant"


def _module_constants(tree: ast.Module) -> dict[str, object]:
    return {
        target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
    }


def _value(node: ast.expr, constants: dict[str, object]) -> object:
    """A literal, or an f-string whose fields name module constants."""
    if not isinstance(node, ast.JoinedStr):
        return ast.literal_eval(node)
    parts = []
    for part in node.values:
        if isinstance(part, ast.FormattedValue):
            assert isinstance(part.value, ast.Name), ast.dump(part)
            parts.append(str(constants[part.value.id]))
        else:
            parts.append(ast.literal_eval(part))
    return "".join(parts)


def _assistant_value(file: str, name: str) -> object:
    """``name``'s value in the assistant's ``file``, at module or class level.

    Skipped only while the assistant has no token check (no ``auth.py``); from
    then on a value it no longer defines fails here, never skips.
    """
    if not (ASSISTANT / "auth.py").exists():
        pytest.skip(f"{ASSISTANT / 'auth.py'} does not exist yet: no token check")
    tree = ast.parse((ASSISTANT / file).read_text())
    constants = _module_constants(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return _value(node.value, constants)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return _value(node.value, constants)
    raise AssertionError(f"{name} not found in {ASSISTANT / file}")


def test_client_id_happy_path() -> None:
    assert _assistant_value("auth.py", "CLIENT_ID") == ASSISTANT_CLIENT_ID


def test_resource_url_happy_path() -> None:
    expected = Settings.model_fields["assistant_resource_url"].default
    assert _assistant_value("settings.py", "resource_url") == expected
