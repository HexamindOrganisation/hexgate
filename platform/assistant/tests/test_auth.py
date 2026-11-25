import asyncio
import base64
import json
import logging

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from hexgate_assistant.auth import ACCESS_TOKEN_TYPE, JwksTokenVerifier, authenticate
from hexgate_assistant.jwks import JwksCache
from tests.conftest import (
    ISSUER,
    JWKS_URL,
    MCP_RESOURCE,
    ORG,
    OTHER_PROJECT,
    PROJECT,
    RESOURCE,
    SUB,
    SigningKey,
    jwks,
)


def _verifier(http: httpx.AsyncClient) -> JwksTokenVerifier:
    return JwksTokenVerifier(
        keys=JwksCache(JWKS_URL, http), issuer=ISSUER, audience=RESOURCE
    )


@pytest.fixture
def jwks_route(key):
    with respx.mock:
        yield respx.get(JWKS_URL).respond(json=jwks(key))


@pytest.fixture
async def verifier(jwks_route):
    async with httpx.AsyncClient() as http:
        yield _verifier(http)


async def test_verify_token_happy_path(verifier, key):
    token = key.mint()

    access = await verifier.verify_token(token)

    assert access is not None
    assert access.token == token
    assert access.subject == SUB
    assert access.org == ORG
    assert access.scopes == ("policy:read", "audit:read", "agents:read")
    assert access.projects == (PROJECT,)
    assert token not in repr(access)


@pytest.mark.parametrize(
    "overrides",
    [
        # Every token an MCP client holds names the MCP server and the API.
        pytest.param({"aud": [MCP_RESOURCE, ISSUER]}, id="aud-mcp-and-public"),
        pytest.param({"aud": MCP_RESOURCE}, id="aud-mcp"),
        pytest.param({"aud": None}, id="missing-aud"),
        pytest.param({"iss": "https://evil.example"}, id="wrong-iss"),
        pytest.param({"client_id": "https://client.example/m.json"}, id="client"),
        pytest.param({"client_id": None}, id="missing-client-id"),
        pytest.param({"exp": 1}, id="expired"),
        pytest.param({"exp": None}, id="missing-exp"),
        pytest.param({"iss": None}, id="missing-iss"),
        pytest.param({"sub": None}, id="missing-sub"),
        pytest.param({"sub": ""}, id="empty-sub"),
        pytest.param({"sub": 2}, id="sub-not-a-string"),
        pytest.param({"org": None}, id="missing-org"),
        pytest.param({"org": ""}, id="empty-org"),
        pytest.param({"org": 5}, id="org-not-a-string"),
        pytest.param({"scope": None}, id="missing-scope"),
        pytest.param({"scope": ["policy:read"]}, id="scope-not-a-string"),
        pytest.param({"projects": []}, id="empty-projects"),
        pytest.param({"projects": None}, id="missing-projects"),
        pytest.param({"projects": PROJECT}, id="projects-not-a-list"),
        pytest.param({"projects": [1]}, id="project-not-a-string"),
    ],
)
async def test_when_claim_is_wrong_then_token_refused(verifier, key, overrides, caplog):
    caplog.set_level(logging.INFO, logger="hexgate_assistant.auth")
    token = key.mint(**overrides)

    assert await verifier.verify_token(token) is None
    assert "token refused" in caplog.text
    assert token not in caplog.text


@pytest.mark.parametrize("typ", ["JWT", "application/at+jwt", "AT+JWT"])
async def test_when_header_typ_is_not_at_jwt_then_refused(
    verifier, jwks_route, key, typ
):
    assert await verifier.verify_token(key.mint(typ=typ)) is None
    assert jwks_route.call_count == 0


async def test_when_signed_by_another_key_under_known_kid_then_refused(verifier, key):
    assert await verifier.verify_token(SigningKey(key.kid).mint()) is None


async def test_when_kid_is_unknown_then_refused(verifier, key):
    assert await verifier.verify_token(key.mint(kid="other-kid")) is None


async def test_when_header_has_no_kid_then_refused(verifier, jwks_route, key):
    _, rest = key.mint().split(".", 1)
    # Re-encode the header without `kid`; the signature no longer matters.
    bare = base64.urlsafe_b64encode(
        json.dumps({"alg": "EdDSA", "typ": ACCESS_TOKEN_TYPE}).encode()
    ).rstrip(b"=")

    assert await verifier.verify_token(f"{bare.decode()}.{rest}") is None
    assert jwks_route.call_count == 0


async def test_when_token_is_not_a_jwt_then_refused_without_fetch(
    verifier, jwks_route, caplog
):
    caplog.set_level(logging.INFO, logger="hexgate_assistant.auth")

    assert await verifier.verify_token("not-a-jwt") is None
    assert jwks_route.call_count == 0
    assert "not-a-jwt" not in caplog.text


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"aud": [MCP_RESOURCE, ISSUER]}, id="aud-mcp-and-public"),
        pytest.param({"iss": "https://evil.example"}, id="wrong-iss"),
    ],
)
async def test_when_aud_or_iss_is_wrong_then_logged_as_warning(
    verifier, key, overrides, caplog
):
    caplog.set_level(logging.INFO, logger="hexgate_assistant.auth")

    assert await verifier.verify_token(key.mint(**overrides)) is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


# authenticate, over HTTP: a throwaway app standing in for the turn route.


@pytest.fixture
def client(jwks_route):
    http = httpx.AsyncClient()
    verifier = _verifier(http)

    async def endpoint(request: Request) -> JSONResponse:
        access = await authenticate(request, verifier, request.path_params["pid"])
        return JSONResponse({"sub": access.subject})

    yield TestClient(Starlette(routes=[Route("/p/{pid}", endpoint)]))
    asyncio.run(http.aclose())


def _get(client, authorization: str | None, project: str = PROJECT):
    headers = {"Authorization": authorization} if authorization is not None else {}
    return client.get(f"/p/{project}", headers=headers)


def test_authenticate_happy_path(client, key):
    response = _get(client, f"Bearer {key.mint()}")

    assert response.status_code == 200
    assert response.json() == {"sub": SUB}


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"aud": [MCP_RESOURCE, ISSUER]}, id="aud-mcp-and-public"),
        pytest.param({"client_id": "https://client.example/m.json"}, id="client"),
        pytest.param({"exp": 1}, id="expired"),
    ],
)
def test_when_token_is_refused_then_401(client, key, overrides):
    response = _get(client, f"Bearer {key.mint(**overrides)}")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("algorithm", ["none", "HS256"])
def test_when_token_is_not_signed_with_eddsa_then_401(client, key, algorithm):
    secret = None if algorithm == "none" else "a-shared-secret-of-32-bytes-long"
    token = key.mint(algorithm=algorithm, secret=secret)

    assert _get(client, f"Bearer {token}").status_code == 401


def test_when_token_uses_a_published_non_eddsa_key_then_401(client, jwks_route, key):
    # The JWKS also publishes an RSA key, so only the verifier's algorithm
    # allowlist stands between an RS256 token signed with it and acceptance.
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwks_route.respond(json={"keys": [*jwks(key)["keys"], {**public, "kid": "rsa-1"}]})
    claims = jwt.decode(key.mint(), options={"verify_signature": False})
    headers = {"kid": "rsa-1", "typ": ACCESS_TOKEN_TYPE}
    token = jwt.encode(claims, private, algorithm="RS256", headers=headers)

    assert _get(client, f"Bearer {token}").status_code == 401


@pytest.mark.parametrize(
    "authorization", [None, "", "Bearer", "Bearer   ", "Basic dXNlcjpwYXNz"]
)
def test_when_no_bearer_token_then_401_without_fetch(client, jwks_route, authorization):
    assert _get(client, authorization).status_code == 401
    assert jwks_route.call_count == 0


def test_when_bearer_scheme_is_lowercase_then_accepted(client, key):
    assert _get(client, f"bearer {key.mint()}").status_code == 200


def test_when_project_is_not_in_the_token_then_403(client, key, caplog):
    caplog.set_level(logging.INFO, logger="hexgate_assistant.auth")
    token = key.mint()

    response = _get(client, f"Bearer {token}", project=OTHER_PROJECT)

    assert response.status_code == 403
    assert token not in caplog.text
