"""The OAuth bearer branch of ``require_user``.

ROUTE declares no scope, so a valid token gets a 403 there; the 401s below
come from the token or its user, before any scope is looked at. The mark
itself (a list of scopes, the project check, the no-project tag) is varied on
a hand-marked route in its own app. Tokens are hand-minted: nothing in the API
mints one yet.
"""

from __future__ import annotations

import json
import logging
import time

import jwt
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from jwt.utils import base64url_encode

from hexgate_api.constants import (
    DEFAULT_PROJECT_ID,
    DEFAULT_USER_ID,
    OAUTH_NO_PROJECT,
    OAUTH_SCOPE_KEY,
)
from hexgate_api.core.db import get_session
from hexgate_api.core.keystore import FileKeyStore
from hexgate_api.core.oauth_tokens import ACCESS_TOKEN_TTL_SECONDS
from hexgate_api.deps.identity import require_user
from hexgate_api.models import User
from hexgate_api.settings import get_settings
from tests.features.oauth.conftest import mint

# A route behind require_user (via require_org_member) that declares no scope.
ROUTE = f"/v1/projects/{DEFAULT_PROJECT_ID}/agents"


def _resign(token: str, sign, **claim_overrides) -> str:
    """``token`` with its claims changed and signed by ``sign``: what mint never makes."""
    header, claims = (
        jwt.get_unverified_header(token),
        jwt.decode(token, options={"verify_signature": False}),
    )
    signing_input = b".".join(
        base64url_encode(json.dumps(part).encode())
        for part in (header, claims | claim_overrides)
    )
    return (signing_input + b"." + base64url_encode(sign(signing_input))).decode()


def _get(client: TestClient, token: str, route: str = ROUTE, **headers: str):
    url = route.replace("{project_id}", DEFAULT_PROJECT_ID)
    return client.get(url, headers={"Authorization": f"Bearer {token}", **headers})


def test_when_token_is_valid_then_a_route_with_no_scope_refuses_it(
    client: TestClient,
) -> None:
    """A valid token is still refused on a route that declares no scope."""
    r = _get(client, mint())
    assert r.status_code == 403
    assert r.json()["detail"] == "token scope does not cover this route"
    assert r.headers["WWW-Authenticate"] == 'Bearer error="insufficient_scope"'


@pytest.mark.parametrize(
    "authorization",
    ["Bearer fty_dev_p1_notabiscuit", "Basic Zm9vOmJhcg=="],
    ids=["sdk-key", "basic"],
)
def test_when_header_is_not_an_oauth_bearer_then_require_user_ignores_it(
    client: TestClient, authorization: str
) -> None:
    """An ``fty_`` envelope is left to the SDK path, not parsed as a JWT."""
    r = client.get(
        ROUTE, headers={"Authorization": authorization, "X-Dev-User": DEFAULT_USER_ID}
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"now": int(time.time()) - ACCESS_TOKEN_TTL_SECONDS - 1},
        {"audience": "https://other.example/mcp"},
        {"audience": get_settings().mcp_resource_url},
        {"issuer": "https://other.example"},
    ],
    ids=["expired", "wrong-aud", "mcp-only-aud", "wrong-iss"],
)
def test_when_token_does_not_verify_then_401(client: TestClient, overrides) -> None:
    r = _get(client, mint(**overrides))
    assert r.status_code == 401
    assert r.json()["detail"] == "missing or invalid authentication"
    assert r.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'


def test_when_signed_by_the_sdk_key_then_401(
    client: TestClient, oauth_keystore: FileKeyStore
) -> None:
    """The SDK root key signs biscuits and sessions; it never passes as the OAuth key."""
    r = _get(client, _resign(mint(), oauth_keystore.sign))
    assert r.status_code == 401


async def test_when_user_is_inactive_then_401(
    client: TestClient, session_factory
) -> None:
    async with session_factory() as session:
        user = await session.get(User, DEFAULT_USER_ID)
        user.is_active = False
        session.add(user)
        await session.commit()
    r = _get(client, mint())
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'


def test_when_user_does_not_exist_then_401(client: TestClient) -> None:
    r = _get(client, mint(sub="no-such-user"))
    assert r.status_code == 401


def test_when_token_is_invalid_then_the_dev_header_does_not_rescue_it(
    client: TestClient,
) -> None:
    """A bearer that is ours but fails is a 401, not a fall-through to X-Dev-User."""
    r = _get(client, "not-a-jwt", **{"X-Dev-User": DEFAULT_USER_ID})
    assert r.status_code == 401


def test_when_token_does_not_decode_then_only_the_error_type_is_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """PyJWT's message can quote the undecodable header; the type says enough."""
    with caplog.at_level(logging.INFO, logger="hexgate_api.deps.identity"):
        _get(client, "not-a-jwt")
    assert "oauth token refused: DecodeError" in caplog.text
    assert "segments" not in caplog.text


def test_when_scheme_is_lowercase_then_the_token_is_still_verified(
    client: TestClient,
) -> None:
    """The scheme is case-insensitive (RFC 9110 §11.1), so ``bearer`` is not ignored."""
    r = client.get(
        ROUTE,
        headers={"Authorization": "bearer not-a-jwt", "X-Dev-User": DEFAULT_USER_ID},
    )
    assert r.status_code == 401


MARKED = "/p/{project_id}"


def _marked_client(
    session_factory, path: str = MARKED, extra: dict | None = None
) -> TestClient:
    """A hand-marked route in its own app, to vary the mark itself."""
    marked = FastAPI()

    @marked.get(path, openapi_extra=extra or {OAUTH_SCOPE_KEY: "policy:read"})
    async def endpoint(request: Request, user: User = Depends(require_user)) -> dict:
        oauth = request.state.oauth
        return {"user": user.id, "jti": oauth.jti if oauth else None}

    async def override_session():
        async with session_factory() as session:
            yield session

    marked.dependency_overrides[get_session] = override_session
    return TestClient(marked)


@pytest.mark.parametrize(
    ("mark", "status"),
    [
        (["policy:read", "audit:read"], 200),
        (["policy:read", "agents:read"], 403),
        ("audit:read", 200),
        ([], 403),
    ],
    ids=["list-all-held", "list-one-missing", "str", "empty-list"],
)
def test_when_route_declares_scopes_then_the_token_must_hold_all(
    session_factory, oauth_keystore, mark, status: int
) -> None:
    client = _marked_client(session_factory, extra={OAUTH_SCOPE_KEY: mark})
    r = _get(client, mint(scopes=["policy:read", "audit:read"]), MARKED)
    assert r.status_code == status, r.text


@pytest.mark.parametrize(
    ("path", "tag", "projects", "status"),
    [
        (MARKED, {}, [DEFAULT_PROJECT_ID], 200),
        (MARKED, {}, ["other"], 403),
        ("/np", {OAUTH_NO_PROJECT: True}, ["other"], 200),
        ("/np/{project_id}", {OAUTH_NO_PROJECT: True}, ["other"], 403),
        ("/x", {}, [DEFAULT_PROJECT_ID], 403),
    ],
    ids=[
        "project-named",
        "project-not-named",
        "tagged-no-project",
        "tag-does-not-skip-a-project-id",
        "untagged-no-project",
    ],
)
def test_when_route_is_scoped_then_the_token_must_name_its_project(
    session_factory, oauth_keystore, path, tag, projects, status: int
) -> None:
    """The tag only excuses a route with no ``project_id``; one with it is always checked."""
    client = _marked_client(
        session_factory, path, {OAUTH_SCOPE_KEY: "policy:read"} | tag
    )
    r = _get(client, mint(projects=projects), path)
    assert r.status_code == status, r.text
    if status == 403:
        assert r.json()["detail"] == "token does not cover this project"


def test_request_state_oauth_holds_an_accepted_token_and_none_otherwise(
    session_factory, oauth_keystore, caplog: pytest.LogCaptureFixture
) -> None:
    client = _marked_client(session_factory)
    token = mint()
    jti = jwt.decode(token, options={"verify_signature": False})["jti"]
    with caplog.at_level(logging.INFO, logger="hexgate_api.deps.identity"):
        assert _get(client, token, MARKED).json()["jti"] == jti
    assert (
        f"oauth token accepted: sub={DEFAULT_USER_ID} "
        f"client_id=https://client.example/meta.json jti={jti}"
    ) in caplog.text
    url = MARKED.replace("{project_id}", DEFAULT_PROJECT_ID)
    r = client.get(url, headers={"X-Dev-User": DEFAULT_USER_ID})
    assert r.json()["jti"] is None, r.text
