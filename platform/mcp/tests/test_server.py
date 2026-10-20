import httpx
import respx
from starlette.testclient import TestClient

from hexgate_mcp.scopes import OAUTH_SCOPES
from hexgate_mcp.server import build_app
from hexgate_mcp.settings import Settings
from tests.conftest import API_URL, ISSUER, JWKS_URL, RESOURCE, jwks

SETTINGS = Settings(api_url=API_URL, issuer=ISSUER, resource_url=RESOURCE)
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


def client() -> TestClient:
    app = build_app(SETTINGS, httpx.AsyncClient())
    return TestClient(app, base_url="https://app.hexgate.test")


def initialize(c: TestClient, token: str | None = None) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return c.post("/mcp", json=INITIALIZE, headers=headers)


@respx.mock
def test_when_no_token_then_401_names_resource_metadata():
    with client() as c:
        response = initialize(c)

    assert response.status_code == 401
    assert f'resource_metadata="{METADATA_URL}"' in response.headers["www-authenticate"]


@respx.mock
def test_when_token_is_refused_then_401(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    token = key.mint(aud="https://other.example/mcp")

    with client() as c:
        response = initialize(c, token)

    assert response.status_code == 401


@respx.mock
def test_when_token_is_valid_then_initialize_succeeds(key):
    respx.get(JWKS_URL).respond(json=jwks(key))

    with client() as c:
        response = initialize(c, key.mint())

    assert response.status_code == 200
    assert '"serverInfo"' in response.text


def test_protected_resource_metadata_happy_path():
    with client() as c:
        response = c.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    assert response.json()["resource"] == RESOURCE
    assert response.json()["authorization_servers"] == [f"{ISSUER}/"]
    assert set(response.json()["scopes_supported"]) == OAUTH_SCOPES
    assert len(c.app.router.routes) == len(
        {getattr(r, "path", None) for r in c.app.router.routes}
    )


def test_health_happy_path():
    with client() as c:
        response = c.get("/health")

    assert response.json() == {"status": "ok", "service": "hexgate-mcp"}
