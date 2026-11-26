"""Verifies the platform API's OAuth access tokens against its JWKS, for the
dashboard's assistant only.

This check is the boundary between a request and our model provider's key:
the API re-checks the token on every MCP call a turn makes, but nothing
re-checks it before the model runs. So a token is accepted only when it was
minted for this service (`aud` names the assistant's own resource URL, never
the MCP server's, which every token an MCP client holds names) and for the
dashboard's assistant client (`client_id`), which only the API's token route
issues, after its verified-email, `Origin` and assistant-mode checks.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

import jwt
from starlette.exceptions import HTTPException
from starlette.requests import Request

from hexgate_assistant.jwks import JwksCache

logger = logging.getLogger(__name__)

# The access-token header `typ` (RFC 9068), the exact string the API writes
# (its core/oauth_tokens.py; copied, since this package never imports
# hexgate_api). It keeps any other JWT the API signs from passing as one.
ACCESS_TOKEN_TYPE = "at+jwt"
# The `client_id` the API's assistant token route writes. No client row
# exists for it; the string is compared.
CLIENT_ID = "hexgate-dashboard-assistant"


@dataclass(frozen=True)
class AssistantToken:
    subject: str
    # The org the project belongs to; the spend of a turn is counted under it.
    org: str
    # Never empty: a grant that names no project can do nothing.
    projects: tuple[str, ...]
    scopes: tuple[str, ...]
    expires_at: int
    # Forwarded to the MCP server; kept out of repr so a logged token object
    # never prints it.
    token: str = field(repr=False)


class JwksTokenVerifier:
    def __init__(self, *, keys: JwksCache, issuer: str, audience: str) -> None:
        self._keys = keys
        self._issuer = issuer
        self._audience = audience

    async def verify_token(self, token: str) -> AssistantToken | None:
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
            # validly signed token is either a token minted for another
            # service (an MCP client's) or HEXGATE_PUBLIC_URL /
            # HEXGATE_ASSISTANT_RESOURCE_URL disagreeing with the API; anything
            # else is an expired or forged token.
            config = (jwt.InvalidIssuerError, jwt.InvalidAudienceError)
            level = logging.WARNING if isinstance(exc, config) else logging.INFO
            logger.log(level, "token refused: %s: %s", type(exc).__name__, exc)
            return None
        # jwt.decode cannot pin a custom claim.
        client_id = claims.get("client_id")
        if client_id != CLIENT_ID:
            logger.info("token refused: client_id %r", str(client_id)[:64])
            return None
        return _assistant_token(token, claims)


async def authenticate(
    request: Request, verifier: JwksTokenVerifier, project_id: str
) -> AssistantToken:
    """The request's bearer token, once verified and granted `project_id`.

    Raises 401 for a missing or refused token (the dashboard fetches a new
    one) and 403 for a valid token that does not name the project.
    """
    raw = _bearer(request)
    access = await verifier.verify_token(raw) if raw is not None else None
    if access is None:
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})
    if project_id not in access.projects:
        logger.info("project %r not granted to sub %s", project_id[:64], access.subject)
        raise HTTPException(403, "project not granted to this token")
    return access


def _bearer(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


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


def _assistant_token(token: str, claims: dict[str, Any]) -> AssistantToken | None:
    subject = claims["sub"]  # a string: jwt.decode checks its type
    org = claims.get("org")
    scope = claims.get("scope")
    projects = claims.get("projects")
    if not subject:
        logger.info("token refused: claims: `sub` is empty")
        return None
    if not isinstance(org, str) or not org:
        logger.info("token refused: claims: `org` is missing or not an id")
        return None
    if not isinstance(scope, str):
        logger.info("token refused: claims: `scope` is not a string")
        return None
    if (
        not isinstance(projects, list)
        or not projects
        or not all(isinstance(p, str) for p in projects)
    ):
        logger.info("token refused: claims: `projects` is not a list of ids")
        return None
    return AssistantToken(
        subject=subject,
        org=org,
        projects=tuple(projects),
        scopes=tuple(scope.split()),
        expires_at=claims["exp"],
        token=token,
    )
