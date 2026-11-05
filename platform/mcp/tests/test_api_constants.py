"""Constants copied from the API, since this package never imports
``hexgate_api``: each test fails when a copy drifts from the API's source."""

import ast
from pathlib import Path

import pytest

from hexgate_mcp.auth import ACCESS_TOKEN_TYPE
from hexgate_mcp.scopes import OAUTH_SCOPES
from hexgate_mcp.tools.policy import ENTRY_FILE, PolicyDraft
from tests.test_policy import FULL_DRY_RUN

API = Path(__file__).parents[2] / "api" / "hexgate_api"


def _api_constant(path: Path, name: str) -> ast.expr:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    raise AssertionError(f"{name} not found in {path}")


def test_oauth_scopes_happy_path():
    # OAUTH_SCOPES = frozenset({...})
    node = _api_constant(API / "constants.py", "OAUTH_SCOPES")
    assert OAUTH_SCOPES == ast.literal_eval(node.args[0])


def test_access_token_type_happy_path():
    node = _api_constant(API / "core" / "oauth_tokens.py", "ACCESS_TOKEN_TYPE")
    assert ACCESS_TOKEN_TYPE == ast.literal_eval(node)


def test_entry_file_happy_path():
    service = API / "features" / "policy_modules" / "service.py"
    assert ENTRY_FILE == ast.literal_eval(_api_constant(service, "ENTRY_FILE"))


def _api_fields(name: str) -> set[str]:
    """The field names of a request model in the API's schemas.py."""
    for node in ast.walk(ast.parse((API / "schemas.py").read_text())):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return {
                s.target.id
                for s in node.body
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
            }
    raise AssertionError(f"{name} not found in schemas.py")


# Each request body the policy tools build, beside the API model reading it:
# the API drops a key it doesn't know, so a renamed field is silently ignored.
REQUEST_KEYS = [
    ("PolicyFileDraft", set(PolicyDraft.model_fields)),
    ("PolicyCheckRequest", {"drafts"}),
    # Every argument set, so every key the tool sends (test_policy asserts it).
    ("PolicyTestRequest", set(FULL_DRY_RUN)),
]


@pytest.mark.parametrize(("model", "keys"), REQUEST_KEYS)
def test_policy_request_keys_happy_path(model, keys):
    assert keys <= _api_fields(model)
