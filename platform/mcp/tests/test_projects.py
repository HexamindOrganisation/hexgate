import json

import pytest
import respx

from tests.conftest import API_URL, JWKS_URL, PROJECT, jwks, mcp_client, post_mcp

OTHER_PROJECT = "00000000-0000-0000-0000-000000000004"


def project(id: str, org_id: str) -> dict[str, str]:
    return {"id": id, "org_id": org_id, "name": f"project {id[-1]}"}


def call_projects_list(token: str) -> dict:
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "projects_list", "arguments": {}},
    }
    with mcp_client() as c:
        response = post_mcp(c, call, token)
    data = next(
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    )
    return json.loads(data)["result"]


def project_url(project_id: str) -> str:
    return f"{API_URL}/v1/projects/{project_id}"


@respx.mock
def test_projects_list_happy_path(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    first = respx.get(project_url(PROJECT)).respond(json=project(PROJECT, "org-1"))
    respx.get(project_url(OTHER_PROJECT)).respond(json=project(OTHER_PROJECT, "org-2"))
    token = key.mint(projects=[PROJECT, OTHER_PROJECT])

    result = call_projects_list(token)

    assert not result["isError"]
    assert json.loads(result["content"][0]["text"]) == [
        project(PROJECT, "org-1"),
        project(OTHER_PROJECT, "org-2"),
    ]
    assert "structuredContent" not in result
    assert first.calls.last.request.headers["authorization"] == f"Bearer {token}"


@pytest.mark.parametrize("status", [403, 404])
@respx.mock
def test_when_a_granted_project_is_refused_then_the_others_are_listed(key, status):
    respx.get(JWKS_URL).respond(json=jwks(key))
    respx.get(project_url(PROJECT)).respond(json=project(PROJECT, "org-1"))
    respx.get(project_url(OTHER_PROJECT)).respond(status, json={"detail": "gone"})

    result = call_projects_list(key.mint(projects=[PROJECT, OTHER_PROJECT]))

    assert not result["isError"]
    assert json.loads(result["content"][0]["text"]) == [project(PROJECT, "org-1")]


@respx.mock
def test_when_every_granted_project_is_refused_then_the_refusal_is_the_error(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    respx.get(project_url(PROJECT)).respond(404, json={"detail": "Not Found"})

    result = call_projects_list(key.mint())

    assert result["isError"]
    assert result["content"][0]["text"].endswith("404 not found: Not Found")


@respx.mock
def test_when_the_api_refuses_the_token_then_the_tool_fails(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    respx.get(project_url(PROJECT)).respond(401, json={"detail": "expired"})

    result = call_projects_list(key.mint())

    assert result["isError"]
    assert result["content"][0]["text"].endswith("401 not authenticated: expired")


@respx.mock
def test_when_listed_then_projects_list_is_read_only(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    tools = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}

    with mcp_client() as c:
        response = post_mcp(c, tools, key.mint())

    data = response.text.split("data: ", 1)[1]
    listed = {t["name"]: t for t in json.loads(data)["result"]["tools"]}
    assert listed["projects_list"]["annotations"]["readOnlyHint"] is True


@respx.mock
def test_when_token_lacks_projects_read_then_refused_before_any_request(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    api = respx.route(host="api.internal")

    result = call_projects_list(key.mint(scope="policy:read"))

    assert result["isError"]
    assert "'projects:read'" in result["content"][0]["text"]
    assert not api.called
