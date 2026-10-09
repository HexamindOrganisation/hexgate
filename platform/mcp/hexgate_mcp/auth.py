"""Verifies the platform API's OAuth access tokens against its JWKS.

This check is for a clean 401 and the client's re-login. The API verifies the
same token on every call it receives, and that check is the security boundary.
"""

import logging
from typing import Annotated, Any

import jwt
from mcp.server.auth.provider import AccessToken
from pydantic import Field, ValidationError

from hexgate_mcp.jwks import JwksCache

logger = logging.getLogger(__name__)


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
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            return None
        if not isinstance(kid, str):
            logger.info("token refused: no `kid` in its header")
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
            # validly signed token means HEXGATE_MCP_ISSUER or _RESOURCE_URL
            # disagrees with the API, which otherwise shows up as nothing but
            # a re-login loop; anything else is an expired or forged token.
            config = (jwt.InvalidIssuerError, jwt.InvalidAudienceError)
            level = logging.WARNING if isinstance(exc, config) else logging.INFO
            logger.log(level, "token refused: %s: %s", type(exc).__name__, exc)
            return None
        return _access_token(token, claims)


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
