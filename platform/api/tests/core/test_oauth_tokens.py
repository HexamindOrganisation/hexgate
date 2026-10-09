"""Tests for OAuth access-token mint and verify."""

from __future__ import annotations

import json
import time
from pathlib import Path

import jwt
import pytest
from jwt.utils import base64url_encode

from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.keystore import FileKeyStore
from hexgate_api.core.oauth_tokens import (
    ACCESS_TOKEN_TTL_SECONDS,
    InvalidAccessTokenError,
    mint_access_token,
    verify_access_token,
)

ISSUER = "https://app.hexgate.test"
AUDIENCE = "https://app.hexgate.test/mcp"


@pytest.fixture(autouse=True)
def oauth_keystore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FileKeyStore:
    ks = FileKeyStore(base_dir=tmp_path / "keystore")
    ks.ensure_keypair()
    monkeypatch.setattr(keystore_mod, "keystore", ks)
    return ks


def _mint(**overrides) -> str:
    kwargs = {
        "sub": "user-1",
        "client_id": "https://client.example/meta.json",
        "scopes": ["policy:read", "audit:read"],
        "projects": ["p1", "p2"],
        "issuer": ISSUER,
        "audience": AUDIENCE,
    } | overrides
    return mint_access_token(**kwargs)


def _claims(**overrides) -> dict:
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": "user-1",
        "aud": AUDIENCE,
        "client_id": "c",
        "scope": "policy:read",
        "projects": ["p1"],
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL_SECONDS,
        "jti": "j",
    } | overrides


def _verify(token: str):
    return verify_access_token(token, issuer=ISSUER, audience=AUDIENCE)


def _jws(header: dict, claims: dict, sign=lambda _: b"") -> str:
    """Assemble a compact JWS by hand, for tokens mint would never produce."""
    signing_input = b".".join(
        base64url_encode(json.dumps(p).encode()) for p in (header, claims)
    )
    return (signing_input + b"." + base64url_encode(sign(signing_input))).decode()


def _oauth_header(ks: FileKeyStore) -> dict:
    return {"alg": "EdDSA", "typ": "JWT", "kid": ks.oauth_fingerprint()}


def test_mint_access_token_happy_path() -> None:
    claims = _verify(_mint())

    assert claims.sub == "user-1"
    assert claims.client_id == "https://client.example/meta.json"
    assert claims.scopes == frozenset({"policy:read", "audit:read"})
    assert claims.projects == frozenset({"p1", "p2"})
    assert claims.jti


def test_when_minted_then_expiry_is_ten_minutes_after_issue() -> None:
    now = int(time.time())
    assert _verify(_mint(now=now)).exp == now + 600


def test_when_minted_then_header_names_the_oauth_key(
    oauth_keystore: FileKeyStore,
) -> None:
    header = jwt.get_unverified_header(_mint())
    assert header["alg"] == "EdDSA"
    assert header["kid"] == oauth_keystore.oauth_fingerprint()


def test_when_projects_repeat_then_the_claim_lists_each_id_once() -> None:
    """Each id once, so the token stays small."""
    token = _mint(projects=["p1", "p2", "p1"])
    assert jwt.decode(token, options={"verify_signature": False})["projects"] == [
        "p1",
        "p2",
    ]


@pytest.mark.parametrize("projects", [[], pytest.param([""], id="empty-csv-split")])
def test_when_projects_name_no_id_then_mint_refuses(projects) -> None:
    """A grant that lost its projects must fail loudly, not hand out a dead token."""
    with pytest.raises(ValueError, match="at least one project"):
        _mint(projects=projects)


@pytest.mark.parametrize("field", ["projects", "scopes"])
def test_when_a_csv_string_is_passed_then_mint_refuses(field) -> None:
    """Iterating "p1,p2" would sign one id per character."""
    with pytest.raises(TypeError, match="csv string"):
        _mint(**{field: "p1,p2"})


@pytest.mark.parametrize("projects", [None, []])
def test_when_projects_claim_absent_or_empty_then_verify_yields_no_projects(
    oauth_keystore: FileKeyStore, projects
) -> None:
    """Absent or empty means no project, never every project."""
    claims = _claims(projects=projects)
    if projects is None:
        del claims["projects"]
    token = _jws(_oauth_header(oauth_keystore), claims, oauth_keystore.oauth_sign)
    assert _verify(token).projects == frozenset()


def test_when_expired_then_refused() -> None:
    with pytest.raises(InvalidAccessTokenError):
        _verify(_mint(now=int(time.time()) - ACCESS_TOKEN_TTL_SECONDS - 5))


def test_when_audience_differs_then_refused() -> None:
    with pytest.raises(InvalidAccessTokenError):
        _verify(_mint(audience="https://other.example/mcp"))


def test_when_issuer_differs_then_refused() -> None:
    with pytest.raises(InvalidAccessTokenError):
        _verify(_mint(issuer="https://evil.example"))


def test_when_signed_by_another_oauth_key_then_refused(
    oauth_keystore: FileKeyStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = FileKeyStore(base_dir=tmp_path / "other")
    other.ensure_keypair()
    monkeypatch.setattr(keystore_mod, "keystore", other)
    token = _mint()
    monkeypatch.setattr(keystore_mod, "keystore", oauth_keystore)

    with pytest.raises(InvalidAccessTokenError, match="unknown key"):
        _verify(token)


def test_when_signed_by_the_root_key_under_the_oauth_kid_then_refused(
    oauth_keystore: FileKeyStore,
) -> None:
    """The SDK key signs biscuits, sessions and bundles; it must never pass as the OAuth key."""
    token = _jws(_oauth_header(oauth_keystore), _claims(), oauth_keystore.sign)
    with pytest.raises(InvalidAccessTokenError):
        _verify(token)


def test_when_claims_tampered_then_refused() -> None:
    """Widening only the projects claim after signing breaks the signature."""
    token = _mint()
    header, _, signature = token.split(".")
    claims = jwt.decode(token, options={"verify_signature": False})
    claims["projects"] = [*claims["projects"], "p9"]
    widened = base64url_encode(
        json.dumps(claims, separators=(",", ":")).encode()
    ).decode()
    with pytest.raises(InvalidAccessTokenError):
        _verify(f"{header}.{widened}.{signature}")


def test_when_alg_none_then_refused(oauth_keystore: FileKeyStore) -> None:
    header = {"alg": "none", "kid": oauth_keystore.oauth_fingerprint()}
    with pytest.raises(InvalidAccessTokenError):
        _verify(_jws(header, _claims()))


def test_when_not_a_jwt_then_refused() -> None:
    with pytest.raises(InvalidAccessTokenError):
        _verify("not-a-token")


def test_when_alg_is_hs256_keyed_with_the_public_key_then_refused(
    oauth_keystore: FileKeyStore,
) -> None:
    """The public key is published; an HMAC token keyed with it must not verify."""
    header = {"alg": "HS256", "typ": "JWT", "kid": oauth_keystore.oauth_fingerprint()}
    token = jwt.encode(
        _claims(), oauth_keystore.oauth_public_key_bytes(), "HS256", header
    )
    with pytest.raises(InvalidAccessTokenError):
        _verify(token)
