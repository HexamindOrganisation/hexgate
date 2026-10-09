"""Ed25519 keys generated per test, and a minter for tokens shaped like the API's."""

import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm

ISSUER = "https://app.hexgate.test"
RESOURCE = "https://app.hexgate.test/mcp"
API_URL = "http://api.internal:8000"
JWKS_URL = f"{API_URL}/.well-known/jwks.json"
PROJECT = "00000000-0000-0000-0000-000000000003"


class SigningKey:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = Ed25519PrivateKey.generate()

    def jwk(self) -> dict[str, str]:
        public = OKPAlgorithm.to_jwk(self.private.public_key(), as_dict=True)
        return {**public, "kid": self.kid, "use": "sig"}

    def mint(self, *, kid: str | None = None, **overrides: Any) -> str:
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
            headers={"kid": kid or self.kid},
        )


def jwks(*keys: SigningKey) -> dict[str, list[dict[str, str]]]:
    return {"keys": [k.jwk() for k in keys]}


@pytest.fixture
def key() -> SigningKey:
    return SigningKey("key-1")
