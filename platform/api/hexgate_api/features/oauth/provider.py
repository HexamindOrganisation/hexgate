"""The OAuth authorization server: the MCP SDK's routes over our provider.

The MCP SDK's handlers parse and check the ``/authorize`` and ``/token`` requests
(response type, PKCE method, the client's redirect and scopes) and format the
standard errors; the provider answers what only the platform knows: it loads
clients, sends an accepted login to the dashboard's consent page, and swaps
the code the consent POST wrote (``router.py``) for the first tokens, then
rotates the refresh token at each use. A token used twice means it leaked, so
its whole login is revoked; a code redeemed twice revokes what it issued.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
)
from mcp.server.auth.handlers.revoke import RevocationErrorResponse
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.json_response import PydanticJSONResponse
from mcp.server.auth.middleware.client_auth import (
    AuthenticationError,
    ClientAuthenticator,
)
from mcp.server.auth.routes import (
    AUTHORIZATION_PATH,
    REVOCATION_PATH,
    TOKEN_PATH,
    create_auth_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from hexgate_api.constants import OAUTH_PREFIX
from hexgate_api.core import db
from hexgate_api.features.oauth.authorization import (
    CheckedAuthorization,
    check_authorization,
    check_authorization_request,
)
from hexgate_api.features.oauth.client_cache import ClientMetadataCache
from hexgate_api.features.oauth.client_metadata import ClientMetadataError
from hexgate_api.features.oauth.grants import (
    as_utc,
    find_refresh_token,
    issue_tokens,
    projects_of,
    redeem_code,
    revoke_family,
    revoke_replayed_code,
    rotate_refresh_token,
    scopes_of,
)
from hexgate_api.models import OAuthRefreshToken
from hexgate_api.settings import get_settings

CONSENT_PATH = "/oauth/consent"


class GrantedCode(AuthorizationCode):
    """A redeemed code, with what the tokens carry beyond the MCP SDK's fields."""

    id: str
    resource: str  # always stored: /authorize refuses a request without it
    user_id: str
    projects: list[str]


class StoredRefreshToken(RefreshToken):
    """A refresh token's row, loaded even when revoked so that its reuse can
    revoke the login."""

    row: OAuthRefreshToken


class HexgateOAuthProvider(
    OAuthAuthorizationServerProvider[GrantedCode, StoredRefreshToken, AccessToken]
):
    def __init__(
        self,
        clients: ClientMetadataCache,
        *,
        resource_url: str,
        consent_url: str,
    ) -> None:
        self._clients = clients
        self._resource_url = resource_url
        self._consent_url = consent_url

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        # None, not the error: the MCP SDK then answers "client not found" itself,
        # where a raised error would be a 500.
        try:
            return await self._clients.get(client_id)
        # The cache logs a refusal once, when it caches the failure.
        except ClientMetadataError:
            return None

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Return the consent page's URL, carrying the checked request.

        Nothing is stored: the consent POST checks the client, redirect and
        scopes again before it writes a code.
        """
        checked = check_authorization(client, params, resource_url=self._resource_url)
        # The whole request: the consent routes check it again as /authorize did.
        query = {
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": str(checked.redirect_uri),
            "scope": " ".join(checked.scopes),
            "code_challenge": checked.code_challenge,
            "code_challenge_method": "S256",
            "resource": checked.resource,
        }
        if checked.state is not None:
            query["state"] = checked.state
        return f"{self._consent_url}?{urlencode(query)}"

    async def check_request(self, params: Mapping[str, Any]) -> CheckedAuthorization:
        """Check an authorization request sent back by the consent page, as
        ``/authorize`` checked it; raises ``AuthorizeError``."""
        return await check_authorization_request(
            params, get_client=self.get_client, resource_url=self._resource_url
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> GrantedCode | None:
        """Spend the code and return it, or None if it is unknown, used or expired.

        Spent here, before the MCP SDK compares the client, ``redirect_uri`` and
        PKCE verifier, so a request that fails any of them burns the code.
        """
        async with db.async_session_factory() as session:
            row = await redeem_code(session, authorization_code)
            if row is None:
                await revoke_replayed_code(session, authorization_code)
                return None
        return GrantedCode(
            id=row.id,
            code=authorization_code,
            scopes=scopes_of(row),
            expires_at=as_utc(row.expires_at).timestamp(),
            client_id=row.client_id,
            code_challenge=row.code_challenge,
            # Stored normalised; the MCP SDK normalises the /token one to compare.
            redirect_uri=AnyUrl(row.redirect_uri),
            redirect_uri_provided_explicitly=True,
            resource=row.resource,
            user_id=row.user_id,
            projects=projects_of(row),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: GrantedCode
    ) -> OAuthToken:
        async with db.async_session_factory() as session:
            return await issue_tokens(
                session,
                user_id=authorization_code.user_id,
                client_id=client.client_id,
                client_name=client.client_name or client.client_id,
                scopes=authorization_code.scopes,
                projects=authorization_code.projects,
                resource=authorization_code.resource,
                authorization_code_id=authorization_code.id,
            )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> StoredRefreshToken | None:
        """The token's row, revoked rows included; None if it is unknown.

        The MCP SDK refuses a token of another client, or a live one past its
        expiry, before ``exchange_refresh_token`` sees it.
        """
        async with db.async_session_factory() as session:
            row = await find_refresh_token(session, refresh_token)
        if row is None:
            return None
        return StoredRefreshToken(
            token=refresh_token,
            client_id=row.client_id,
            scopes=scopes_of(row),
            # None for a revoked row, so the MCP SDK's expiry check doesn't
            # answer for it: an expired reuse still revokes the login.
            expires_at=None
            if row.revoked_at
            else int(as_utc(row.expires_at).timestamp()),
            row=row,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: StoredRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate the token. The new one carries the row's scopes whatever
        ``scopes`` asks: the MCP SDK has already refused a wider request, and
        a grant is never narrowed piecemeal."""
        row = refresh_token.row
        async with db.async_session_factory() as session:
            if row.revoked_at is None:
                tokens = await rotate_refresh_token(session, row)
                if tokens is not None:
                    return tokens
            # Revoked, or revoked by a concurrent refresh since it loaded: a reuse.
            await revoke_family(session, row.family_id)
        raise TokenError("invalid_grant", "refresh token was already used")

    async def revoke_token(self, token: StoredRefreshToken) -> None:
        """Revoke the token's whole login. Only ``/revoke`` calls this, with a
        refresh token: access tokens are not stored, so not revocable one by one."""
        async with db.async_session_factory() as session:
            await revoke_family(session, token.row.family_id, user_id=token.row.user_id)


def build_oauth_provider() -> HexgateOAuthProvider:
    """The provider the OAuth routes share, so they share one client cache."""
    settings = get_settings()
    clients = ClientMetadataCache(
        allow_insecure_localhost=settings.oauth_allow_insecure_client_ids
    )
    return HexgateOAuthProvider(
        clients,
        resource_url=settings.mcp_resource_url,
        consent_url=f"{settings.dashboard_url}{CONSENT_PATH}",
    )


def oauth_routes(provider: HexgateOAuthProvider) -> list[Route]:
    """The authorize, token and revocation routes under ``/v1/oauth``, over ``provider``.

    Not the MCP SDK's metadata route: ``well_known.py`` serves that document,
    with an ``issuer`` that equals the tokens' ``iss``. The authorize route
    takes GET only (no HEAD), so the consent POST can have that path. Token and
    revocation carry no allow-any-origin CORS wrapper, so the app's
    ``CORSMiddleware`` alone decides which origins may call them; both answer
    401 ``unauthorized_client`` for a ``client_id`` whose document doesn't load.
    """
    settings = get_settings()
    (authorize,) = [
        route
        for route in create_auth_routes(
            provider,
            issuer_url=AnyHttpUrl(settings.public_url),
            client_registration_options=ClientRegistrationOptions(enabled=False),
        )
        if route.path == AUTHORIZATION_PATH
    ]
    authorize_route = Route(
        f"{OAUTH_PREFIX}{AUTHORIZATION_PATH}", authorize.endpoint, methods=["GET"]
    )
    # Starlette adds HEAD to every GET route; the MCP SDK's handler would read it as a form POST.
    authorize_route.methods.discard("HEAD")
    authenticator = ClientAuthenticator(provider)
    token_route = Route(
        f"{OAUTH_PREFIX}{TOKEN_PATH}",
        TokenHandler(provider, authenticator).handle,
        methods=["POST"],
    )
    revoke_route = Route(
        f"{OAUTH_PREFIX}{REVOCATION_PATH}",
        _revocation_endpoint(provider, authenticator),
        methods=["POST"],
    )
    return [authorize_route, token_route, revoke_route]


def _revocation_endpoint(
    provider: HexgateOAuthProvider, authenticator: ClientAuthenticator
):
    """RFC 7009 revocation for public clients.

    Not the MCP SDK's handler: its request model requires ``client_secret``,
    so it answers 400 to a public client, which sends none.
    """

    async def revoke(request: Request) -> Response:
        try:
            client = await authenticator.authenticate_request(request)
        except AuthenticationError as exc:
            return PydanticJSONResponse(
                RevocationErrorResponse(
                    error="unauthorized_client", error_description=exc.message
                ),
                status_code=401,
            )
        token = (await request.form()).get("token")
        if not isinstance(token, str) or not token:
            return PydanticJSONResponse(
                RevocationErrorResponse(
                    error="invalid_request", error_description="token is required"
                ),
                status_code=400,
            )
        stored = await provider.load_refresh_token(client, token)
        # Another client's token, or none: 200 all the same (RFC 7009 section 2.2).
        if stored is not None and stored.client_id == client.client_id:
            await provider.revoke_token(stored)
        return Response(
            status_code=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
        )

    return revoke
