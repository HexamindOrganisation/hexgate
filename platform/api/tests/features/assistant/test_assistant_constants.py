"""Values the assistant copies from the API, since it never imports
``hexgate_api``: each test fails when its copy drifts from the API's."""

import ast
from pathlib import Path

from hexgate_api.constants import ASSISTANT_CLIENT_ID
from hexgate_api.settings import Settings

# The assistant's source, read without importing it.
ASSISTANT = Path(__file__).parents[4] / "assistant" / "hexgate_assistant"


def _assistant_constant(path: Path, name: str) -> ast.expr:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
        # A dataclass field: `resource_url: str = ...`.
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return node.value
    raise AssertionError(f"{name} not found in {path}")


def test_client_id_happy_path():
    node = _assistant_constant(ASSISTANT / "auth.py", "CLIENT_ID")
    assert ASSISTANT_CLIENT_ID == ast.literal_eval(node)


def test_resource_url_happy_path():
    settings = ASSISTANT / "settings.py"
    port = ast.literal_eval(_assistant_constant(settings, "PORT"))
    # resource_url: str = f"http://localhost:{PORT}/v1/assistant"
    node = _assistant_constant(settings, "resource_url")
    assert ast.unparse(node) == "f'http://localhost:{PORT}/v1/assistant'"
    default = Settings.model_fields["assistant_resource_url"].default
    assert default == f"http://localhost:{port}/v1/assistant"
