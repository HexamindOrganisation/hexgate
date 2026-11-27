import json

import httpx
import pytest
import respx
from mcp.server.fastmcp import FastMCP

from hexgate_mcp.api import ApiClient
from hexgate_mcp.tools import policy_apply
from tests.conftest import API_URL, JWKS_URL, PROJECT, call_tool, jwks
from tests.test_policy import api_model_fields

APPLY_URL = f"{API_URL}/v1/projects/{PROJECT}/policy/apply"
FILE = {"name": "policy.yaml", "content": "version: 1\n", "base_hash": "abc"}
SAMPLE = {"role": "analyst", "tool": "search", "expect": "allow"}
APPLIED = {"applied": True, "change_id": "c1", "checks": [], "files": []}


def apply(key, **extra) -> dict:
    arguments = {"project_id": PROJECT, "files": [FILE], "summary": "why"} | extra
    return call_tool(key.mint(scope="policy:apply"), "policy_apply", arguments)


@respx.mock
def test_policy_apply_happy_path(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    route = respx.post(APPLY_URL).respond(json=APPLIED)

    result = apply(key, samples=[SAMPLE])

    assert json.loads(result["content"][0]["text"]) == APPLIED
    request = route.calls.last.request
    assert json.loads(request.content) == {
        "files": [FILE],
        "summary": "why",
        "samples": [SAMPLE | {"agent": "*", "args": {}, "attributes": {}}],
        "session_id": None,
    }
    assert request.headers["authorization"].startswith("Bearer ey")


@respx.mock
def test_when_apply_is_sent_then_its_keys_are_api_model_fields(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    route = respx.post(APPLY_URL).respond(json=APPLIED)

    apply(key, samples=[SAMPLE], session_id="s1")
    body = json.loads(route.calls.last.request.content)

    # Equal, not a subset: a field the API gains must be offered or left out
    # on purpose, and `extra=forbid` refuses a misspelt one.
    assert set(body) == api_model_fields("PolicyApplyRequest")
    assert set(body["files"][0]) == api_model_fields("PolicyApplyFile") | {"content"}
    assert set(body["samples"][0]) == api_model_fields("PolicyApplySample")


@respx.mock
def test_when_apply_is_sent_then_it_waits_for_the_answer_past_the_client_default(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    route = respx.post(APPLY_URL).respond(json=APPLIED)

    apply(key)

    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout["read"] == policy_apply.APPLY_TIMEOUT_SECONDS >= 30


@pytest.mark.parametrize(
    ("status", "detail", "text"),
    [
        (409, {"code": "stale", "files": ["policy.yaml"]}, "409 conflict: "),
        (409, {"code": "layout"}, "409 conflict: "),
        (403, {"code": "assistant_mode"}, "403 forbidden: "),
        (422, "bad", "422 invalid request: "),
    ],
)
@respx.mock
def test_when_api_refuses_then_tool_error_carries_its_detail(key, status, detail, text):
    respx.get(JWKS_URL).respond(json=jwks(key))
    respx.post(APPLY_URL).respond(status, json={"detail": detail})

    result = apply(key)

    shown = detail if isinstance(detail, str) else json.dumps(detail)
    assert result["isError"]
    assert result["content"][0]["text"] == (
        f"Error executing tool policy_apply: {text}{shown}"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"samples": [{"role": "r", "tool": "t", "expect": "allow", "arguments": {}}]},
        {"files": [FILE | {"basehash": "abc"}]},
    ],
    ids=["sample", "file"],
)
@respx.mock
def test_when_a_file_or_sample_has_an_unknown_key_then_refused_before_any_request(
    key, arguments
):
    respx.get(JWKS_URL).respond(json=jwks(key))
    api = respx.route(host="api.internal", path__startswith="/v1/")

    result = apply(key, **arguments)

    assert result["isError"]
    assert not api.called


@respx.mock
def test_when_a_check_fails_then_the_answer_says_nothing_was_saved(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    failed = {"applied": False, "change_id": None, "checks": [{"id": "bundle"}]}
    respx.post(APPLY_URL).respond(json=failed)

    result = apply(key)

    assert not result.get("isError")
    assert json.loads(result["content"][0]["text"]) == failed


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("slow"), httpx.Response(500, text="boom")],
    ids=["timeout", "server-error"],
)
@respx.mock
def test_when_the_apply_gets_no_answer_then_error_says_it_may_be_saved(key, failure):
    # The API commits before it answers, so the change may be saved; a blind
    # retry would then spend an attempt on the 409 stale.
    respx.get(JWKS_URL).respond(json=jwks(key))
    route = respx.post(APPLY_URL)
    if isinstance(failure, Exception):
        route.mock(side_effect=failure)
    else:
        route.mock(return_value=failure)

    result = apply(key)

    assert result["isError"]
    assert result["content"][0]["text"] == (
        f"Error executing tool policy_apply: {policy_apply.NO_ANSWER}"
    )


async def test_description_tells_the_model_a_timed_out_apply_may_be_saved():
    mcp = FastMCP("test")
    policy_apply.register(mcp, ApiClient(API_URL, httpx.AsyncClient()))
    [tool] = await mcp.list_tools()

    text = " ".join(tool.description.split())
    assert "may be saved" in text
    assert "409 `stale`" in text
