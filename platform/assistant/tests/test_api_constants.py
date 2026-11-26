"""Constants copied from the API and the MCP server, since this package
imports neither: each test fails when a copy drifts from its source."""

import ast
from pathlib import Path

from hexgate_assistant.auth import ACCESS_TOKEN_TYPE
from hexgate_assistant.changes import (
    APPLY_CALL_SECONDS,
    APPLY_TIMEOUT_SECONDS,
    CONFLICT_PREFIX,
    NO_ANSWER,
    SERVER_PREFIX,
)
from hexgate_assistant.context import TRUNCATED_MARKER

# The API's source, read without importing it.
API = Path(__file__).parents[2] / "api" / "hexgate_api"
MCP = Path(__file__).parents[2] / "mcp" / "hexgate_mcp"


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


def test_truncated_marker_happy_path():
    node = _api_constant(MCP / "tools" / "__init__.py", "TRUNCATED_MARKER")
    assert TRUNCATED_MARKER == ast.literal_eval(node)


def test_apply_timeout_seconds_happy_path():
    node = _api_constant(MCP / "tools" / "policy_apply.py", "APPLY_TIMEOUT_SECONDS")
    assert APPLY_TIMEOUT_SECONDS == ast.literal_eval(node)
    # The server's limit ends first; test_changes shows what reaches the
    # model when ours ends first instead, as it does at equal limits.
    assert APPLY_CALL_SECONDS > APPLY_TIMEOUT_SECONDS


def test_no_answer_happy_path():
    node = _api_constant(MCP / "tools" / "policy_apply.py", "NO_ANSWER")
    assert NO_ANSWER == ast.literal_eval(node)


def test_server_prefix_happy_path():
    # The platform's MCP server's own test pins the prefix its SDK puts on
    # every tool error; the copy here must match it.
    pinned = (MCP.parent / "tests" / "test_policy_apply.py").read_text()
    assert f'f"{SERVER_PREFIX}' in pinned


def test_conflict_prefix_happy_path():
    # The MCP server words a refusal "<status> <reason>: <detail>", the
    # reason from TOOL_ERRORS.
    api = MCP / "api.py"
    reasons = ast.literal_eval(_api_constant(api, "TOOL_ERRORS"))
    assert CONFLICT_PREFIX == f"409 {reasons[409]}: "
    assert 'f"{response.status_code} {reason}: {_detail(response)}"' in api.read_text()
