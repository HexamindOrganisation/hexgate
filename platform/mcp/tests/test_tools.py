import json

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.fastmcp.exceptions import ToolError

from hexgate_mcp.auth import HexgateAccessToken
from hexgate_mcp.tools import (
    RESULT_CAP_BYTES,
    TRUNCATED_MARKER,
    caller,
    result_text,
)
from tests.conftest import PROJECT


@pytest.fixture
def signed_in():
    token = HexgateAccessToken(
        token="t",
        client_id="c",
        scopes=["policy:read", "audit:read"],
        subject="u",
        projects=[PROJECT],
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    yield token
    auth_context_var.reset(reset)


def test_caller_happy_path(signed_in):
    assert caller("policy:read", "audit:read", project_id=PROJECT) is signed_in


def test_when_one_of_several_scopes_is_missing_then_refused(signed_in):
    with pytest.raises(ToolError, match="'agents:read' scope"):
        caller("policy:read", "agents:read")


def test_when_project_is_not_granted_then_refused(signed_in):
    with pytest.raises(ToolError, match="project 'other' is not one of the projects"):
        caller("policy:read", project_id="other")


def test_when_no_token_then_refused():
    with pytest.raises(ToolError, match="not authenticated"):
        caller("policy:read")


def test_when_tool_names_no_scope_then_it_raises(signed_in):
    with pytest.raises(ValueError, match="must name the scope"):
        caller(project_id=PROJECT)


def test_result_text_happy_path():
    assert json.loads(result_text({"a": [1, 2]})) == {"a": [1, 2]}


def test_when_result_is_exactly_the_cap_then_it_is_sent_whole():
    value = "x" * (RESULT_CAP_BYTES - 2)  # json.dumps adds the two quotes

    assert result_text(value) == json.dumps(value)


def test_when_result_exceeds_the_cap_then_head_is_kept_with_marker():
    value = "x" * RESULT_CAP_BYTES

    text = result_text(value)

    assert len(text.encode()) == RESULT_CAP_BYTES
    assert text.startswith('"xxx')
    assert text.endswith(TRUNCATED_MARKER)


def test_when_cut_falls_inside_a_character_then_it_is_dropped():
    # "é" is two bytes, and the opening quote puts the even cut inside one.
    assert len(TRUNCATED_MARKER) % 2 == 0
    value = "é" * RESULT_CAP_BYTES

    text = result_text(value)

    assert len(text.encode()) == RESULT_CAP_BYTES - 1
    assert text.endswith("é" + TRUNCATED_MARKER)


def test_when_list_exceeds_the_cap_then_whole_items_are_kept():
    item = {"id": "p", "name": "x" * 1000}
    value = [item] * 300  # ~300 KiB

    text = result_text(value)

    assert len(text.encode()) <= RESULT_CAP_BYTES
    result = json.loads(text)
    assert result["truncated"] is True
    assert 0 < len(result["items"]) < len(value)
    assert result["items"] == value[: len(result["items"])]
    # One more item would not have fit.
    one_more = json.dumps(
        {"truncated": True, "items": value[: len(result["items"]) + 1]},
        ensure_ascii=False,
    )
    assert len(one_more.encode()) > RESULT_CAP_BYTES


def test_when_first_list_item_exceeds_the_cap_then_head_is_kept():
    value = ["x" * RESULT_CAP_BYTES]

    text = result_text(value)

    assert len(text.encode()) == RESULT_CAP_BYTES
    assert text.startswith('["xxx')
    assert text.endswith(TRUNCATED_MARKER)
