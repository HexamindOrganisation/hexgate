"""Constants copied from the API, since this package never imports
``hexgate_api``: each test fails when a copy drifts from the API's source."""

import ast
from pathlib import Path

from hexgate_assistant.auth import ACCESS_TOKEN_TYPE

# The API's source, read without importing it.
API = Path(__file__).parents[2] / "api" / "hexgate_api"


def _api_constant(path: Path, name: str) -> ast.expr:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    raise AssertionError(f"{name} not found in {path}")


def test_access_token_type_happy_path():
    node = _api_constant(API / "core" / "oauth_tokens.py", "ACCESS_TOKEN_TYPE")
    assert ACCESS_TOKEN_TYPE == ast.literal_eval(node)
