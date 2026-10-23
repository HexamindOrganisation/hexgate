"""Ed25519 keys generated per test, a minter for tokens shaped like the API's,
and a client for the app over its MCP transport."""

import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm
from starlette.testclient import TestClient

from hexgate_mcp.auth import ACCESS_TOKEN_TYPE
from hexgate_mcp.server import build_app
from hexgate_mcp.settings import Settings

ISSUER = "https://app.hexgate.test"
RESOURCE = "https://app.hexgate.test/mcp"
API_URL = "http://api.internal:8000"
JWKS_URL = f"{API_URL}/.well-known/jwks.json"
PROJECT = "00000000-0000-0000-0000-000000000003"
SETTINGS = Settings(api_url=API_URL, issuer=ISSUER, resource_url=RESOURCE)


class SigningKey:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = Ed25519PrivateKey.generate()

    def jwk(self) -> dict[str, str]:
        public = OKPAlgorithm.to_jwk(self.private.public_key(), as_dict=True)
        return {**public, "kid": self.kid, "use": "sig"}

    def mint(
        self, *, kid: str | None = None, typ: str = ACCESS_TOKEN_TYPE, **overrides: Any
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": "00000000-0000-0000-0000-000000000002",
            "aud": RESOURCE,
            "client_id": "https://client.example/metadata.json",
            "scope": "policy:read projects:read",
            "projects": [PROJECT],
            "iat": now,
            "exp": now + 600,
            "jti": str(uuid.uuid4()),
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(
            claims,
            self.private,
            algorithm="EdDSA",
            headers={"kid": kid or self.kid, "typ": typ},
        )


def jwks(*keys: SigningKey) -> dict[str, list[dict[str, str]]]:
    return {"keys": [k.jwk() for k in keys]}


@pytest.fixture
def key() -> SigningKey:
    return SigningKey("key-1")


def mcp_client() -> TestClient:
    app = build_app(SETTINGS, httpx.AsyncClient())
    return TestClient(app, base_url="https://app.hexgate.test")


def post_mcp(c: TestClient, body: dict[str, Any], token: str | None) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return c.post("/mcp", json=body, headers=headers)
