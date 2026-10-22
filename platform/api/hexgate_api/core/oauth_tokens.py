"""OAuth access tokens: ten-minute EdDSA JWTs signed with the keystore's OAuth key.

The OAuth key is never the root key, so an SDK token, session or bundle
signature never verifies here; ``kid`` names the signing key.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterable
from typing import Annotated

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jwt.utils import base64url_encode
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
)

ACCESS_TOKEN_TTL_SECONDS = 600
# The JWS ``alg`` every access token carries. The OAuth JWKS
# (``/.well-known/jwks.json``) is to publish the key under it, with ``kid`` =
# ``keystore.oauth_fingerprint()`` — never ``/v1/.well-known/keys``, whose
# first key the SDK pins as the root key.
ACCESS_TOKEN_ALGORITHM = "EdDSA"
# The JWS ``typ`` of an access token (RFC 9068). Verifiers require it, so no
# other JWT the API signs can pass as an access token.
ACCESS_TOKEN_TYPE = "at+jwt"


class InvalidAccessTokenError(Exception):
    """The bearer is not an access token this API issued for this audience."""


class AccessTokenClaims(BaseModel):
    """The verified claims of an access token, the whole set. Validating a
    decoded payload against it refuses any claim of the wrong type, so a bad
    shape is a 401, never a 500 or a set of characters. PyJWT has already
    checked the values of iss, aud, exp and iat."""

    model_config = ConfigDict(frozen=True)

    iss: str
    aud: str | list[str]  # RFC 7519 allows a list
    sub: str
    client_id: str
    # The `scope` claim, a space-separated string.
    scopes: frozenset[str] = Field(validation_alias="scope")
    # The projects this grant covers. Never empty, as in the MCP verifier: a
    # grant naming no project can do nothing, and must never read as every one.
    projects: Annotated[frozenset[str], Field(min_length=1)]
    jti: str
    iat: StrictInt
    exp: StrictInt  # "123" is refused, not coerced

    @field_validator("scopes", mode="before")
    @classmethod
    def _split_scope(cls, scope: object) -> list[str]:
        if not isinstance(scope, str):
            raise ValueError("scope is not a string")
        return scope.split()


def mint_access_token(
    *,
    sub: str,
    client_id: str,
    scopes: Iterable[str],
    projects: Iterable[str],
    issuer: str,
    audience: str,
    now: int | None = None,
) -> str:
    """Return a signed access token valid for ``ACCESS_TOKEN_TTL_SECONDS``.

    A token naming no project (``[]``, or ``[""]`` from ``"".split(",")``)
    means the caller lost the grant's projects, so it is refused rather than
    minted dead; so is a bare csv string, which would iterate as characters.
    """
    if isinstance(projects, str) or isinstance(scopes, str):
        raise TypeError("pass projects and scopes as lists, not a csv string")
    project_ids = [p for p in dict.fromkeys(projects) if p]  # each id once, in order
    if not project_ids:
        raise ValueError("an access token must cover at least one project")
    iat = int(time.time()) if now is None else now
    claims = {
        "iss": issuer,
        "sub": sub,
        "aud": audience,
        "client_id": client_id,
        "scope": " ".join(sorted(set(scopes))),
        "projects": project_ids,
        "iat": iat,
        "exp": iat + ACCESS_TOKEN_TTL_SECONDS,
        "jti": secrets.token_urlsafe(16),
    }
    return _sign_compact(claims)


def verify_access_token(token: str, *, issuer: str, audience: str) -> AccessTokenClaims:
    """Return the claims of ``token`` (typ, kid, EdDSA signature, iss, aud, exp
    checked, and every claim's type), or raise ``InvalidAccessTokenError``."""
    from hexgate_api.core.keystore import keystore

    public_key = Ed25519PublicKey.from_public_bytes(keystore.oauth_public_key_bytes())
    try:
        header = jwt.get_unverified_header(token)
        if header.get("typ") != ACCESS_TOKEN_TYPE:
            raise InvalidAccessTokenError("not an access token (typ)")
        if header.get("kid") != keystore.oauth_fingerprint():
            raise InvalidAccessTokenError("access token signed by an unknown key")
        claims = jwt.decode(
            token,
            public_key,
            algorithms=[ACCESS_TOKEN_ALGORITHM],
            issuer=issuer,
            audience=audience,
            options={"require": ["iss", "sub", "aud", "exp", "iat", "jti"]},
        )
    except jwt.DecodeError as exc:
        # The type only: the message can quote the undecodable input.
        raise InvalidAccessTokenError(type(exc).__name__) from exc
    # TypeError: PyJWT calls int() on exp/iat and lets it escape for, say, an
    # object, which would otherwise be a 500.
    except (jwt.PyJWTError, TypeError) as exc:
        raise InvalidAccessTokenError(str(exc)) from exc
    try:
        return AccessTokenClaims.model_validate(claims)
    except ValidationError as exc:
        raise InvalidAccessTokenError(
            f"claims: {exc.errors(include_input=False)}"
        ) from exc


def _sign_compact(claims: dict) -> str:
    """Encode ``claims`` as a compact JWS, signed inside the keystore like ``keystore.sign``."""
    from hexgate_api.core.keystore import keystore

    header = {
        "alg": ACCESS_TOKEN_ALGORITHM,
        "typ": ACCESS_TOKEN_TYPE,
        "kid": keystore.oauth_fingerprint(),
    }
    signing_input = b".".join(
        base64url_encode(json.dumps(part, separators=(",", ":")).encode())
        for part in (header, claims)
    )
    signature = base64url_encode(keystore.oauth_sign(signing_input))
    return (signing_input + b"." + signature).decode()
