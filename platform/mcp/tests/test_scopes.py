import ast
from pathlib import Path

from hexgate_mcp.scopes import OAUTH_SCOPES

API_CONSTANTS = (
    Path(__file__).parents[2] / "api" / "hexgate_api" / "constants.py"
).read_text()


def _api_scopes() -> set[str]:
    for node in ast.walk(ast.parse(API_CONSTANTS)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "OAUTH_SCOPES" for t in node.targets
        ):
            # OAUTH_SCOPES = frozenset({...})
            return ast.literal_eval(node.value.args[0])
    raise AssertionError("OAUTH_SCOPES not found in the API's constants.py")


def test_oauth_scopes_happy_path():
    assert OAUTH_SCOPES == _api_scopes()
