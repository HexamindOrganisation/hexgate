"""OAuth access tokens: EdDSA JWTs signed with the keystore's OAuth key.

An access token lives ten minutes and is never stored. It names the user
(``sub``), the client, the scopes the user approved and the projects they
ticked, so a verifier can refuse a call with no database lookup.

The OAuth key is not the root key: a token signed by ``keystore.sign`` (SDK
tokens, sessions, bundles) must never verify here, and the ``kid`` header
names which key signed, so a key rotation is visible to the MCP server's
JWKS cache.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jwt.utils import base64url_encode

ACCESS_TOKEN_TTL_SECONDS = 600
# The JWS ``alg`` every access token carries. The OAuth JWKS
# (``/.well-known/jwks.json``) is to publish the key under it, with ``kid`` =
# ``keystore.oauth_fingerprint()`` — never ``/v1/.well-known/keys``, whose
# first key the SDK pins as the root key.
ACCESS_TOKEN_ALGORITHM = "EdDSA"


class InvalidAccessTokenError(Exception):
    """The bearer is not an access token this API issued for this audience."""


@dataclass(frozen=True)
class AccessTokenClaims:
    """The verified claims of an access token."""

    sub: str
    client_id: str
    scopes: frozenset[str]
    # The projects this grant covers. Empty means the token reaches no
    # project — never every project.
    projects: frozenset[str]
    jti: str
    exp: int


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

    ``projects`` comes from the grant's ``projects_csv``, split by the caller.
    A token naming no project is refused here: it could reach nothing, so
    minting it means a caller lost the grant's projects (a refresh that
    forgot to copy them, whose ``"".split(",")`` is ``[""]``), and that should
    fail loudly rather than hand the client a dead token. A bare string is
    refused for the same reason: iterating ``"p1,p2"`` yields characters.
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
    """Return the claims of ``token``, or raise ``InvalidAccessTokenError``.

    Checks the ``kid``, the EdDSA signature against the OAuth public key,
    ``iss``, ``aud`` and ``exp``. A token signed by any other key — the root
    key included — is refused.
    """
    from hexgate_api.core.keystore import keystore

    try:
        kid = jwt.get_unverified_header(token).get("kid")
        if kid != keystore.oauth_fingerprint():
            raise InvalidAccessTokenError("access token signed by an unknown key")
        public_key = Ed25519PublicKey.from_public_bytes(
            keystore.oauth_public_key_bytes()
        )
        claims = jwt.decode(
            token,
            public_key,
            algorithms=[ACCESS_TOKEN_ALGORITHM],
            issuer=issuer,
            audience=audience,
            options={"require": ["iss", "sub", "aud", "exp", "iat", "jti"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidAccessTokenError(str(exc)) from exc
    return AccessTokenClaims(
        sub=claims["sub"],
        client_id=claims["client_id"],
        scopes=frozenset(claims["scope"].split()),
        projects=frozenset(claims.get("projects") or ()),
        jti=claims["jti"],
        exp=claims["exp"],
    )


def _sign_compact(claims: dict) -> str:
    """Encode ``claims`` as a compact JWS signed through ``keystore.oauth_sign``.

    Signed through the keystore rather than ``jwt.encode`` so the private key
    never leaves it, as with ``keystore.sign``; verification stays PyJWT's.
    """
    from hexgate_api.core.keystore import keystore

    header = {
        "alg": ACCESS_TOKEN_ALGORITHM,
        "typ": "JWT",
        "kid": keystore.oauth_fingerprint(),
    }
    signing_input = b".".join(
        base64url_encode(json.dumps(part, separators=(",", ":")).encode())
        for part in (header, claims)
    )
    signature = base64url_encode(keystore.oauth_sign(signing_input))
    return (signing_input + b"." + signature).decode()
