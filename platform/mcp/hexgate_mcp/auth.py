"""Verifies the platform API's OAuth access tokens against its JWKS.

This check is for a clean 401 and the client's re-login. The API is to verify
the same token on every call it receives, as a token minted for itself too
(spec §I), and that check is the security boundary; it lands with plan PR 9
(#403), and until then the API answers the bearer with 401.
"""

import logging
from typing import Annotated, Any

import jwt
from mcp.server.auth.provider import AccessToken
from pydantic import Field, ValidationError

from hexgate_mcp.jwks import JwksCache

logger = logging.getLogger(__name__)

# The access-token header `typ` (RFC 9068), the exact string the API writes
# (its core/oauth_tokens.py; copied, since this package never imports
# hexgate_api). It keeps any other JWT the API signs from passing as one.
# Stricter than RFC 9068 §4, which also allows "application/at+jwt" in any
# case: only the API mints these tokens, and it writes this form.
ACCESS_TOKEN_TYPE = "at+jwt"


class HexgateAccessToken(AccessToken):
    subject: str
    # An empty claim refuses the token rather than meaning "every project": a
    # grant that names no project can do nothing.
    projects: Annotated[list[str], Field(min_length=1)]


class JwksTokenVerifier:
    def __init__(self, *, keys: JwksCache, issuer: str, audience: str) -> None:
        self._keys = keys
        self._issuer = issuer
        self._audience = audience

    async def verify_token(self, token: str) -> HexgateAccessToken | None:
        kid = _header_kid(token)
        if kid is None:
            return None
        key = await self._keys.get(kid)
        if key is None:
            # Forged, or the API signs under a kid its JWKS does not publish.
            logger.info("token refused: kid %r not in the JWKS", kid[:64])
            return None
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["EdDSA"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            # The reason only, never the token. A wrong `iss` or `aud` on a
            # validly signed token means HEXGATE_PUBLIC_URL or
            # HEXGATE_MCP_RESOURCE_URL disagrees with the API, which otherwise
            # shows up as nothing but a re-login loop; anything else is an
            # expired or forged token.
            config = (jwt.InvalidIssuerError, jwt.InvalidAudienceError)
            level = logging.WARNING if isinstance(exc, config) else logging.INFO
            logger.log(level, "token refused: %s: %s", type(exc).__name__, exc)
            return None
        return _access_token(token, claims)


def _header_kid(token: str) -> str | None:
    """The token's `kid`, once its header passes as an access token's."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        # The type only: its message can quote the undecodable header.
        logger.info("token refused: not a JWT: %s", type(exc).__name__)
        return None
    if header.get("typ") != ACCESS_TOKEN_TYPE:
        logger.info("token refused: header typ is not %r", ACCESS_TOKEN_TYPE)
        return None
    kid = header.get("kid")
    if not isinstance(kid, str):
        logger.info("token refused: no `kid` in its header")
        return None
    return kid


def _access_token(token: str, claims: dict[str, Any]) -> HexgateAccessToken | None:
    scope = claims.get("scope")
    if not isinstance(scope, str):
        logger.info("token refused: claims: `scope` is not a string")
        return None
    try:
        return HexgateAccessToken(
            token=token,
            client_id=claims.get("client_id"),
            scopes=scope.split(),
            expires_at=claims["exp"],
            resource=claims["aud"],
            subject=claims["sub"],
            projects=claims.get("projects"),
            claims=claims,
        )
    except ValidationError as exc:
        logger.info("token refused: claims: %s", exc.errors(include_input=False))
        return None
