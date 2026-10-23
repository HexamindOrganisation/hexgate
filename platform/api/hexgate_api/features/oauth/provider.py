"""The OAuth authorization server: the MCP SDK's routes over our provider.

The MCP SDK's handlers parse and check the ``/authorize`` and ``/token`` requests
(response type, PKCE method, the client's redirect and scopes) and format the
standard errors; the provider answers what only the platform knows. This half
loads clients and sends an accepted login to the dashboard's consent page.
Codes and tokens come in later halves: until then no code or refresh token
exists, so ``/token`` answers a known client ``invalid_grant``.
"""

from __future__ import annotations

from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.routes import AUTHORIZATION_PATH, TOKEN_PATH, create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.routing import Route

from hexgate_api.constants import OAUTH_SCOPES
from hexgate_api.features.oauth.client_cache import ClientMetadataCache
from hexgate_api.features.oauth.client_metadata import ClientMetadataError
from hexgate_api.settings import get_settings

OAUTH_PREFIX = "/v1/oauth"
CONSENT_PATH = "/oauth/consent"


class HexgateOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(
        self, clients: ClientMetadataCache, *, resource_url: str, consent_url: str
    ) -> None:
        self._clients = clients
        self._resource_url = resource_url
        self._consent_url = consent_url

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        # None, not the error: the MCP SDK then answers "client not found" itself,
        # where a raised error would be a 500.
        try:
            return await self._clients.get(client_id)
        except ClientMetadataError:
            return None

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Return the consent page's URL, carrying the checked request.

        Nothing is stored: the consent POST checks the client, redirect and
        scopes again before it writes a code.
        """
        # RFC 8707: the token is bound to the one MCP server, and the MCP spec
        # makes clients send it, so a missing resource is refused too. /token
        # never re-checks it: the MCP SDK doesn't pass it to the provider there.
        if params.resource != self._resource_url:
            raise AuthorizeError(
                "invalid_request", f"resource must be {self._resource_url}"
            )
        # No scope asks for every scope, so the consent page shows the whole
        # list and the user decides; a grant of none could do nothing.
        scopes = sorted(OAUTH_SCOPES) if params.scopes is None else params.scopes
        query = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "scope": " ".join(scopes),
            "code_challenge": params.code_challenge,
            "code_challenge_method": "S256",
            "resource": params.resource,
        }
        if params.state is not None:
            query["state"] = params.state
        return f"{self._consent_url}?{urlencode(query)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return None  # No code is written before the consent POST exists.

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        raise NotImplementedError  # Unreachable: no code loads.

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return None  # No refresh token is issued before the code exchange exists.

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        raise NotImplementedError  # Unreachable: no refresh token loads.


def oauth_routes() -> list[Route]:
    """The MCP SDK's authorize and token handlers under ``/v1/oauth``, over one provider.

    Not its metadata route: ``well_known.py`` serves that document, with an
    ``issuer`` that equals the tokens' ``iss``. The authorize route takes GET
    only (no HEAD), so the consent POST can have that path. The token route
    is the MCP SDK's handler without its allow-any-origin CORS wrapper, so the
    app's ``CORSMiddleware`` alone decides which origins may call it.
    """
    settings = get_settings()
    clients = ClientMetadataCache(
        allow_insecure_localhost=settings.oauth_allow_insecure_client_ids
    )
    provider = HexgateOAuthProvider(
        clients,
        resource_url=settings.mcp_resource_url,
        consent_url=f"{settings.dashboard_url.rstrip('/')}{CONSENT_PATH}",
    )
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
