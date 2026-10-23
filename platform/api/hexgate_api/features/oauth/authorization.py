"""Check an authorization request, the one way every route that takes one does.

``/authorize`` gets the MCP SDK's checks, then ``check_authorization``. The
consent routes get the request back from the browser, so
``check_authorization_request`` runs both again: nothing from the GET is stored.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.errors import stringify_pydantic_error
from mcp.server.auth.handlers.authorize import AuthorizationRequest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import (
    InvalidRedirectUriError,
    InvalidScopeError,
    OAuthClientInformationFull,
)
from pydantic import AnyUrl, ValidationError

from hexgate_api.constants import OAUTH_SCOPES

# The state rides in /authorize's redirect to the consent page and back to the
# client. RFC 6749 sets no cap; clients send a few dozen characters, and a
# 60 KB one would make a Location header nginx answers with a 502.
MAX_STATE_LENGTH = 1024
# RFC 7636 §4.2: an S256 challenge is the base64url SHA-256, unpadded.
_S256_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class CheckedAuthorization:
    """An authorization request every check accepted."""

    client: OAuthClientInformationFull
    redirect_uri: AnyUrl
    scopes: list[str]
    code_challenge: str
    resource: str
    state: str | None


async def check_authorization_request(
    params: Mapping[str, Any],
    *,
    get_client: Callable[[str], Awaitable[OAuthClientInformationFull | None]],
    resource_url: str,
) -> CheckedAuthorization:
    """Check ``params`` as the MCP SDK's authorize handler does, then as ``check_authorization``.

    Raises ``AuthorizeError``, with the error code the MCP SDK would answer.
    """
    try:
        request = AuthorizationRequest.model_validate(params)
    except ValidationError as exc:
        # As the MCP SDK does: only a wrong value is unsupported, a missing one invalid.
        unsupported = any(
            e["loc"] == ("response_type",) and e["type"] == "literal_error"
            for e in exc.errors()
        )
        error = "unsupported_response_type" if unsupported else "invalid_request"
        raise AuthorizeError(error, stringify_pydantic_error(exc)) from None
    client = await get_client(request.client_id)
    if client is None:
        raise AuthorizeError("invalid_request", "client_id not found")
    try:
        redirect_uri = client.validate_redirect_uri(request.redirect_uri)
        scopes = client.validate_scope(request.scope)
    except InvalidRedirectUriError as exc:
        raise AuthorizeError("invalid_request", exc.message) from None
    except InvalidScopeError as exc:
        raise AuthorizeError("invalid_scope", exc.message) from None
    auth_params = AuthorizationParams(
        state=request.state,
        scopes=scopes,
        code_challenge=request.code_challenge,
        redirect_uri=redirect_uri,
        redirect_uri_provided_explicitly=True,
        resource=request.resource,
    )
    return check_authorization(client, auth_params, resource_url=resource_url)


def check_authorization(
    client: OAuthClientInformationFull,
    params: AuthorizationParams,
    *,
    resource_url: str,
) -> CheckedAuthorization:
    """Apply the platform's own rules to a request the MCP SDK's checks accepted.

    Raises ``AuthorizeError``.
    """
    # RFC 8707: the token is bound to the one MCP server, and the MCP spec
    # makes clients send it, so a missing resource is refused too. /token
    # never re-checks it: the MCP SDK doesn't pass it to the provider there.
    if params.resource != resource_url:
        raise AuthorizeError("invalid_request", f"resource must be {resource_url}")
    if not _S256_CHALLENGE.fullmatch(params.code_challenge):
        raise AuthorizeError(
            "invalid_request", "code_challenge must be an S256 challenge"
        )
    if params.state is not None and len(params.state) > MAX_STATE_LENGTH:
        raise AuthorizeError(
            "invalid_request", f"state is longer than {MAX_STATE_LENGTH} characters"
        )
    # No scope asks for every scope, so the consent page shows the whole
    # list and the user decides; a grant of none could do nothing.
    scopes = sorted(OAUTH_SCOPES) if params.scopes is None else params.scopes
    return CheckedAuthorization(
        client=client,
        redirect_uri=params.redirect_uri,
        scopes=scopes,
        code_challenge=params.code_challenge,
        resource=resource_url,
        state=params.state,
    )
