import logging

import httpx
import pytest
import respx

from hexgate_mcp.auth import JwksTokenVerifier
from hexgate_mcp.jwks import JwksCache
from tests.conftest import ISSUER, JWKS_URL, PROJECT, RESOURCE, SigningKey, jwks


@pytest.fixture
async def verifier():
    async with httpx.AsyncClient() as http:
        yield JwksTokenVerifier(
            keys=JwksCache(JWKS_URL, http), issuer=ISSUER, audience=RESOURCE
        )


@respx.mock
async def test_verify_token_happy_path(verifier, key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    token = key.mint()

    access = await verifier.verify_token(token)

    assert access is not None
    assert access.token == token
    assert access.scopes == ["policy:read", "projects:read"]
    assert access.projects == [PROJECT]
    assert access.client_id == "https://client.example/metadata.json"
    assert access.resource == RESOURCE
    assert access.claims["iss"] == ISSUER


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"aud": "https://other.example/mcp"}, id="wrong-aud"),
        pytest.param({"iss": "https://evil.example"}, id="wrong-iss"),
        pytest.param({"exp": 1}, id="expired"),
        pytest.param({"exp": None}, id="missing-exp"),
        pytest.param({"iss": None}, id="missing-iss"),
        pytest.param({"aud": None}, id="missing-aud"),
        pytest.param({"projects": []}, id="empty-projects"),
        pytest.param({"projects": None}, id="missing-projects"),
        pytest.param({"projects": "p1"}, id="projects-not-a-list"),
        pytest.param({"projects": [1]}, id="project-not-a-string"),
        pytest.param({"scope": None}, id="missing-scope"),
        pytest.param({"client_id": None}, id="missing-client-id"),
        pytest.param({"sub": None}, id="missing-sub"),
    ],
)
@respx.mock
async def test_when_claim_is_wrong_then_token_refused(verifier, key, overrides, caplog):
    respx.get(JWKS_URL).respond(json=jwks(key))
    caplog.set_level(logging.INFO, logger="hexgate_mcp.auth")
    token = key.mint(**overrides)

    assert await verifier.verify_token(token) is None
    assert "token refused" in caplog.text
    assert token not in caplog.text


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"aud": "https://other.example/mcp"}, id="wrong-aud"),
        pytest.param({"iss": "https://evil.example"}, id="wrong-iss"),
    ],
)
@respx.mock
async def test_when_iss_or_aud_mismatch_then_logged_as_warning(
    verifier, key, overrides, caplog
):
    respx.get(JWKS_URL).respond(json=jwks(key))

    assert await verifier.verify_token(key.mint(**overrides)) is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


@respx.mock
async def test_when_signed_by_another_key_under_known_kid_then_refused(verifier, key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    forger = SigningKey(key.kid)

    assert await verifier.verify_token(forger.mint()) is None


@respx.mock
async def test_when_kid_is_unknown_then_refusal_is_logged(verifier, key, caplog):
    respx.get(JWKS_URL).respond(json=jwks(key))
    caplog.set_level(logging.INFO, logger="hexgate_mcp.auth")

    assert await verifier.verify_token(key.mint(kid="other-kid")) is None
    assert "kid 'other-kid' not in the JWKS" in caplog.text


@respx.mock
async def test_when_token_is_not_a_jwt_then_refused_without_fetch(verifier):
    route = respx.get(JWKS_URL).respond(json={"keys": []})

    assert await verifier.verify_token("fty_not-a-jwt") is None
    assert route.call_count == 0
