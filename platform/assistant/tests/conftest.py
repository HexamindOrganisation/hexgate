"""Ed25519 keys generated per test and a minter for tokens shaped like the
ones the API's assistant token route issues."""

import os
import time
import uuid
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm
from sqlalchemy import event, make_url

from hexgate_assistant.auth import ACCESS_TOKEN_TYPE, CLIENT_ID
from hexgate_assistant.db import make_engine
from hexgate_assistant.models import Base
from hexgate_assistant.settings import Settings

ISSUER = "https://app.hexgate.test"
RESOURCE = "https://app.hexgate.test/v1/assistant"
MCP_RESOURCE = "https://app.hexgate.test/mcp"
API_URL = "http://api.internal:8000"
JWKS_URL = f"{API_URL}/.well-known/jwks.json"
SUB = "00000000-0000-0000-0000-000000000002"
PROJECT = "00000000-0000-0000-0000-000000000003"
OTHER_PROJECT = "00000000-0000-0000-0000-000000000004"
# A Postgres URL (postgresql+asyncpg://...) to run the database tests on
# instead of a per-test SQLite file. Its tables are dropped before each test,
# so point it at a throwaway database.
TEST_DATABASE_URL = os.environ.get("HEXGATE_ASSISTANT_TEST_DATABASE_URL")


class SigningKey:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = Ed25519PrivateKey.generate()

    def jwk(self) -> dict[str, str]:
        public = OKPAlgorithm.to_jwk(self.private.public_key(), as_dict=True)
        return {**public, "kid": self.kid, "use": "sig"}

    def mint(
        self,
        *,
        kid: str | None = None,
        typ: str = ACCESS_TOKEN_TYPE,
        algorithm: str = "EdDSA",
        secret: str | None = None,
        **overrides: Any,
    ) -> str:
        """A token signed with this key, or, given `algorithm` and `secret`,
        the same claims signed some other way (`none`, an HMAC)."""
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": SUB,
            "aud": [RESOURCE, MCP_RESOURCE, ISSUER],
            "client_id": CLIENT_ID,
            "scope": "policy:read audit:read agents:read",
            "projects": [PROJECT],
            "iat": now,
            "exp": now + 600,
            "jti": str(uuid.uuid4()),
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(
            claims,
            self.private if algorithm == "EdDSA" else secret,
            algorithm=algorithm,
            headers={"kid": kid or self.kid, "typ": typ},
        )


def jwks(*keys: SigningKey) -> dict[str, list[dict[str, str]]]:
    return {"keys": [k.jwk() for k in keys]}


@pytest.fixture
def key() -> SigningKey:
    return SigningKey("key-1")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    url = TEST_DATABASE_URL or f"sqlite+aiosqlite:///{tmp_path / 'assistant.db'}"
    return Settings(
        database_url=make_url(url),
        issuer=ISSUER,
        resource_url=RESOURCE,
        api_url=API_URL,
    )


def _foreign_keys_on(dbapi_connection, _record) -> None:
    # SQLite enforces foreign keys, and so cascades, only when asked to.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest.fixture
async def engine(settings):
    engine = make_engine(settings.database_url)
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    else:
        # A shared Postgres keeps tables between tests: start empty.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    yield engine
    await engine.dispose()
