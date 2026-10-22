import httpx
import respx
from starlette.testclient import TestClient

from hexgate_mcp.scopes import OAUTH_SCOPES
from tests.conftest import (
    JWKS_URL,
    RESOURCE,
    SETTINGS,
    jwks,
    mcp_client,
    post_mcp,
    rpc,
)

METADATA_URL = "https://app.hexgate.test/.well-known/oauth-protected-resource/mcp"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def initialize(c: TestClient, token: str | None = None) -> httpx.Response:
    return post_mcp(c, INITIALIZE, token)


@respx.mock
def test_when_no_token_then_401_names_resource_metadata():
    with mcp_client() as c:
        response = initialize(c)

    assert response.status_code == 401
    assert f'resource_metadata="{METADATA_URL}"' in response.headers["www-authenticate"]


@respx.mock
def test_when_token_is_refused_then_401(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    token = key.mint(aud="https://other.example/mcp")

    with mcp_client() as c:
        response = initialize(c, token)

    assert response.status_code == 401


@respx.mock
def test_when_token_is_valid_then_initialize_succeeds(key):
    respx.get(JWKS_URL).respond(json=jwks(key))

    with mcp_client() as c:
        response = initialize(c, key.mint())

    assert response.status_code == 200
    assert '"serverInfo"' in response.text


@respx.mock
def test_when_get_on_mcp_then_405_rather_than_an_idle_stream(key):
    respx.get(JWKS_URL).respond(json=jwks(key))

    with mcp_client() as c:
        response = c.get(
            "/mcp",
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {key.mint()}",
            },
        )

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


def test_when_get_on_mcp_has_no_token_then_401_names_resource_metadata():
    with mcp_client() as c:
        response = c.get("/mcp", headers={"Accept": "text/event-stream"})

    assert response.status_code == 401
    assert f'resource_metadata="{METADATA_URL}"' in response.headers["www-authenticate"]


def test_protected_resource_metadata_happy_path():
    with mcp_client() as c:
        response = c.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    assert response.json()["resource"] == RESOURCE
    assert response.json()["authorization_servers"] == [SETTINGS.issuer]
    assert set(response.json()["scopes_supported"]) == OAUTH_SCOPES
    # /mcp is listed twice on purpose: the GET refusal, then the SDK's route.
    paths = [getattr(r, "path", None) for r in c.app.router.routes]
    assert sorted(paths) == sorted([*set(paths), "/mcp"])


def test_health_happy_path():
    with mcp_client() as c:
        response = c.get("/health")

    assert response.json() == {"status": "ok", "service": "hexgate-mcp"}


@respx.mock
def test_when_tools_are_listed_then_every_one_is_marked_read_only(key):
    # Nothing registered by default writes: clients may call any of them
    # without asking the user first.
    respx.get(JWKS_URL).respond(json=jwks(key))

    tools = rpc(key.mint(), "tools/list", {})["tools"]

    read_only = {"readOnlyHint": True, "openWorldHint": False}
    assert {t["name"]: t["annotations"] for t in tools} == {
        "projects_list": read_only,
        "policy_reference": read_only,
        "policy_list_files": read_only,
        "policy_read_file": read_only,
        "policy_resolve": read_only,
        "policy_graph": read_only,
        "policy_list_roles": read_only,
        "policy_check": read_only,
        "policy_dry_run": read_only,
    }
