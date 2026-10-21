"""The root discovery documents: authorization-server metadata and the OAuth JWKS.

The app is the router plus the SPA catch-all over a fake build, so a route
that fell through to the SPA would answer index.html with a 200.
"""

from __future__ import annotations

from pathlib import Path

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hexgate_api.constants import OAUTH_SCOPES
from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core import spa
from hexgate_api.core.keystore import FileKeyStore
from hexgate_api.core.oauth_tokens import mint_access_token
from hexgate_api.features.oauth.well_known import router
from hexgate_api.settings import get_settings

PUBLIC_URL = "https://app.hexgate.test"
RESOURCE_URL = "https://app.hexgate.test/mcp"


@pytest.fixture(autouse=True)
def oauth_keystore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FileKeyStore:
    ks = FileKeyStore(base_dir=tmp_path / "keystore")
    ks.ensure_keypair()
    monkeypatch.setattr(keystore_mod, "keystore", ks)
    return ks


@pytest.fixture(autouse=True)
def public_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HEXGATE_PUBLIC_URL", PUBLIC_URL)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>app</title>")
    monkeypatch.setenv("HEXGATE_DASHBOARD_DIST", str(dist))
    app = FastAPI()
    app.include_router(router)
    spa.mount_spa(app)
    return TestClient(app)


def test_authorization_server_metadata_happy_path(client: TestClient) -> None:
    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == {
        "issuer": PUBLIC_URL,
        "authorization_endpoint": f"{PUBLIC_URL}/v1/oauth/authorize",
        "token_endpoint": f"{PUBLIC_URL}/v1/oauth/token",
        "revocation_endpoint": f"{PUBLIC_URL}/v1/oauth/revoke",
        "jwks_uri": f"{PUBLIC_URL}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": sorted(OAUTH_SCOPES),
        "client_id_metadata_document_supported": True,
    }


def test_jwks_happy_path(client: TestClient, oauth_keystore: FileKeyStore) -> None:
    resp = client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    (key,) = resp.json()["keys"]
    assert key["kty"] == "OKP"
    assert key["crv"] == "Ed25519"
    assert key["alg"] == "EdDSA"
    assert key["kid"] == oauth_keystore.oauth_fingerprint()


def test_when_jwks_is_served_then_it_holds_the_oauth_key_not_the_root_key(
    client: TestClient, oauth_keystore: FileKeyStore
) -> None:
    (key,) = jwt.PyJWKSet.from_dict(client.get("/.well-known/jwks.json").json()).keys
    raw = key.key.public_bytes_raw()
    assert raw == oauth_keystore.oauth_public_key_bytes()
    assert raw != oauth_keystore.public_key_bytes()


def test_when_jwks_is_parsed_by_pyjwt_then_it_verifies_an_access_token(
    client: TestClient,
) -> None:
    # What the MCP server does: a JWK PyJWT can't use is dropped silently,
    # and every token is then refused.
    key_set = jwt.PyJWKSet.from_dict(client.get("/.well-known/jwks.json").json())
    token = mint_access_token(
        sub="u",
        client_id="c",
        scopes=["policy:read"],
        projects=["p1"],
        issuer=PUBLIC_URL,
        audience=RESOURCE_URL,
    )
    key = key_set[jwt.get_unverified_header(token)["kid"]]
    claims = jwt.decode(token, key, algorithms=["EdDSA"], audience=RESOURCE_URL)
    assert claims["sub"] == "u"


def test_when_well_known_path_is_unknown_then_json_404_not_spa(
    client: TestClient,
) -> None:
    resp = client.get("/.well-known/nope")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


def test_when_app_is_built_then_well_known_is_at_root_not_v1() -> None:
    from hexgate_api.main import create_app

    paths = {getattr(r, "path", None) for r in create_app().routes}
    assert {
        "/.well-known/oauth-authorization-server",
        "/.well-known/jwks.json",
    } <= paths
    assert "/v1/.well-known/oauth-authorization-server" not in paths
