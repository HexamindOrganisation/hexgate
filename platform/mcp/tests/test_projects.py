import json

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


@respx.mock
def test_projects_list_happy_path(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    orgs = respx.get(f"{API_URL}/v1/orgs").respond(
        json=[{"id": "org-1"}, {"id": "org-2"}]
    )
    respx.get(f"{API_URL}/v1/orgs/org-1/projects").respond(
        json=[project(PROJECT, "org-1"), project(OTHER_PROJECT, "org-1")]
    )
    respx.get(f"{API_URL}/v1/orgs/org-2/projects").respond(json=[])
    token = key.mint()

    result = call_projects_list(token)

    assert not result["isError"]
    assert json.loads(result["content"][0]["text"]) == [project(PROJECT, "org-1")]
    assert "structuredContent" not in result
    assert orgs.calls.last.request.headers["authorization"] == f"Bearer {token}"


@respx.mock
def test_when_token_lacks_projects_read_then_refused_before_any_request(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    orgs = respx.get(f"{API_URL}/v1/orgs")

    result = call_projects_list(key.mint(scope="policy:read"))

    assert result["isError"]
    assert "'projects:read'" in result["content"][0]["text"]
    assert not orgs.called
