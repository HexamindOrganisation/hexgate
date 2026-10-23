"""The OAuth authorization server: the MCP SDK's routes over our provider.

The MCP SDK's handlers parse and check the ``/authorize`` and ``/token`` requests
(response type, PKCE method, the client's redirect and scopes) and format the
standard errors; the provider answers what only the platform knows: it loads
clients, sends an accepted login to the dashboard's consent page, and swaps
the code the consent POST wrote (``router.py``) for the first tokens. Refresh
tokens come in a later half: until then none loads, so a refresh answers
``invalid_grant``.
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
)
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.routes import AUTHORIZATION_PATH, TOKEN_PATH, create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl
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
from hexgate_api.features.oauth.grants import as_utc, issue_tokens, redeem_code
from hexgate_api.settings import get_settings

CONSENT_PATH = "/oauth/consent"


class GrantedCode(AuthorizationCode):
    """A redeemed code, with what the tokens carry beyond the MCP SDK's fields."""

    resource: str  # always stored: /authorize refuses a request without it
    user_id: str
    projects: list[str]


class HexgateOAuthProvider(
    OAuthAuthorizationServerProvider[GrantedCode, RefreshToken, AccessToken]
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
            return None
        return GrantedCode(
            code=authorization_code,
            scopes=row.scopes_csv.split(","),
            expires_at=as_utc(row.expires_at).timestamp(),
            client_id=row.client_id,
            code_challenge=row.code_challenge,
            # Stored normalised; the MCP SDK normalises the /token one to compare.
            redirect_uri=AnyUrl(row.redirect_uri),
            redirect_uri_provided_explicitly=True,
            resource=row.resource,
            user_id=row.user_id,
            projects=row.projects_csv.split(","),
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
            )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return None  # Refresh lands in a later half; until then a refresh is invalid_grant.

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        raise NotImplementedError  # Unreachable: no refresh token loads.


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
    """The MCP SDK's authorize and token handlers under ``/v1/oauth``, over ``provider``.

    Not its metadata route: ``well_known.py`` serves that document, with an
    ``issuer`` that equals the tokens' ``iss``. The authorize route takes GET
    only (no HEAD), so the consent POST can have that path. The token route
    is the MCP SDK's handler without its allow-any-origin CORS wrapper, so the
    app's ``CORSMiddleware`` alone decides which origins may call it.
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
    token_route = Route(
        f"{OAUTH_PREFIX}{TOKEN_PATH}",
        TokenHandler(provider, ClientAuthenticator(provider)).handle,
        methods=["POST"],
    )
    return [authorize_route, token_route]
