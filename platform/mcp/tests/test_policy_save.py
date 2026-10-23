import json

import httpx
import pytest
import respx
from mcp.server.fastmcp import FastMCP

from hexgate_mcp.api import ApiClient
from hexgate_mcp.tools import policy_save
from tests.conftest import API_URL, JWKS_URL, PROJECT, call_tool, jwks

FILES_URL = f"{API_URL}/v1/projects/{PROJECT}/policy-files"
SAVED = {"name": "modules/a b?.yaml", "content_hash": "abc"}


def save(key, name: str) -> dict:
    arguments = {"project_id": PROJECT, "name": name, "content": "version: 1\n"}
    return call_tool(key.mint(scope="policy:write"), "policy_save", arguments)


@respx.mock
def test_policy_save_happy_path(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    # Quoted, or the `?` would start a query string.
    route = respx.put(f"{FILES_URL}/modules/a%20b%3F.yaml").respond(json=SAVED)

    result = save(key, "modules/a b?.yaml")

    assert json.loads(result["content"][0]["text"]) == SAVED
    request = route.calls.last.request
    assert json.loads(request.content) == {"content": "version: 1\n"}
    assert request.headers["authorization"].startswith("Bearer ey")


@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, "forbidden"), (409, "conflict"), (422, "invalid request")],
)
@respx.mock
def test_when_api_refuses_then_tool_error_carries_its_detail(key, status, reason):
    respx.get(JWKS_URL).respond(json=jwks(key))
    respx.put(f"{FILES_URL}/policy.yaml").respond(status, json={"detail": "why"})

    result = save(key, name="policy.yaml")

    assert result["isError"]
    assert result["content"][0]["text"] == (
        f"Error executing tool policy_save: {status} {reason}: why"
    )


@pytest.mark.parametrize(
    "name", ["../agents/x", "modules/../../agents/x", "/etc/x", "a//b", ".", ""]
)
@respx.mock
def test_when_name_would_leave_the_route_then_refused_before_any_request(key, name):
    respx.get(JWKS_URL).respond(json=jwks(key))
    api = respx.route(host="api.internal", path__startswith="/v1/")

    result = save(key, name=name)

    assert result["isError"]
    assert "invalid policy file name" in result["content"][0]["text"]
    assert not api.called


async def test_description_warns_that_a_first_policy_yaml_switches_the_project():
    # The dashboard shows a classic project a banner; the model gets this line.
    mcp = FastMCP("test")
    policy_save.register(mcp, ApiClient(API_URL, httpx.AsyncClient()))
    [tool] = await mcp.list_tools()

    assert "replaces that with the composed files" in " ".join(tool.description.split())
