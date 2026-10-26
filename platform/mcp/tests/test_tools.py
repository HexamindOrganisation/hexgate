import json

import pytest
import respx
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.fastmcp.exceptions import ToolError

from hexgate_mcp.auth import HexgateAccessToken
from hexgate_mcp.tools import (
    ANY_NAME,
    RESULT_CAP_BYTES,
    TRUNCATED_MARKER,
    caller,
    result_text,
    untrusted,
)
from tests.conftest import JWKS_URL, PROJECT, call_tool, jwks

OTHER_PROJECT = "00000000-0000-0000-0000-000000000004"


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


@pytest.mark.parametrize("project_id", [PROJECT[:-1], ""])
def test_when_project_only_resembles_a_granted_one_then_refused(signed_in, project_id):
    with pytest.raises(ToolError, match="is not one of the projects"):
        caller("policy:read", project_id=project_id)


def test_when_both_scope_and_project_are_missing_then_the_scope_is_named(signed_in):
    with pytest.raises(ToolError, match="'agents:read' scope"):
        caller("agents:read", project_id=OTHER_PROJECT)


@pytest.mark.parametrize(
    ("tool", "arguments", "scope"),
    [
        ("policy_save", {"name": "policy.yaml", "content": ""}, "policy:write"),
        ("audit_decisions", {}, "audit:read"),
        ("agents_list", {}, "agents:read"),
    ],
)
@pytest.mark.parametrize("grant", ["scope", "project"])
@respx.mock
def test_when_grant_does_not_cover_the_call_then_refused_before_any_request(
    key, tool, arguments, scope, grant
):
    respx.get(JWKS_URL).respond(json=jwks(key))
    api = respx.route(host="api.internal", path__startswith="/v1/")
    if grant == "scope":
        token, project_id, error = key.mint(scope="projects:read"), PROJECT, scope
    else:
        token, project_id, error = key.mint(scope=scope), OTHER_PROJECT, OTHER_PROJECT

    result = call_tool(token, tool, arguments | {"project_id": project_id})

    assert result["isError"]
    assert repr(error) in result["content"][0]["text"]
    assert not api.called


def test_untrusted_happy_path():
    spec = {"name": None, "params": {ANY_NAME: {"type": None}}}
    value = {"name": "n", "params": {"db": {"type": "s", "title": "t"}}, "x": 1}

    assert untrusted(value, spec) == {
        "name": "n",
        "params": {"db": {"type": "s", "untrusted_title": "t"}},
        "untrusted_x": 1,
    }


def test_when_a_trusted_name_appears_at_another_depth_then_it_is_marked():
    spec = {"name": None, "inner": {}}

    assert untrusted([{"name": "n", "inner": {"name": "m"}}], spec) == [
        {"name": "n", "inner": {"untrusted_name": "m"}}
    ]


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


def test_when_a_page_of_small_rows_exceeds_the_cap_then_as_many_as_fit_are_kept():
    # Many small rows: the ", " between them is two of every five bytes.
    page = {"rows": ["r"] * RESULT_CAP_BYTES, "total": RESULT_CAP_BYTES}

    text = result_text(page)

    sent = json.loads(text)
    assert RESULT_CAP_BYTES - len(', "r"') < len(text.encode()) <= RESULT_CAP_BYTES
    assert sent["truncated"] is True
    assert sent["total"] == RESULT_CAP_BYTES


def test_when_first_row_exceeds_the_cap_then_the_page_keeps_no_rows():
    page = {"rows": ["x" * RESULT_CAP_BYTES], "total": 1, "offset": 7}

    assert json.loads(result_text(page)) == {
        "truncated": True,
        "rows": [],
        "total": 1,
        "offset": 7,
        "next_offset": 7,
    }
