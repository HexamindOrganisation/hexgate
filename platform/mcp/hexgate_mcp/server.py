"""The FastMCP app: streamable HTTP at /mcp behind the JWKS token verifier."""

import httpx
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from hexgate_mcp.auth import JwksTokenVerifier
from hexgate_mcp.jwks import JwksCache
from hexgate_mcp.scopes import OAUTH_SCOPES
from hexgate_mcp.settings import Settings


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "hexgate-mcp"})


def build_app(settings: Settings, http: httpx.AsyncClient) -> Starlette:
    verifier = JwksTokenVerifier(
        keys=JwksCache(settings.jwks_url, http),
        issuer=settings.issuer,
        audience=settings.resource_url,
    )
    # The SDK answers an unauthenticated /mcp with a 401 whose
    # WWW-Authenticate names the protected-resource metadata built from these:
    # that sends clients to the API's login.
    auth = AuthSettings(
        issuer_url=settings.issuer,
        resource_server_url=settings.resource_url,
        required_scopes=None,
        # The verifier pins `aud` and reports it as the token's resource; the
        # SDK compares that with resource_server_url as well.
        validate_token_resource=True,
    )
    mcp = FastMCP(
        "hexgate",
        token_verifier=verifier,
        auth=auth,
        stateless_http=True,
        streamable_http_path="/mcp",
        # FastMCP's default host (127.0.0.1) turns on a Host allowlist of
        # localhost only, which would refuse every proxied request. Every /mcp
        # call carries a bearer token, so a rebound page has nothing to send.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    mcp.custom_route("/health", methods=["GET"], include_in_schema=False)(health)
    app = mcp.streamable_http_app()
    _advertise_scopes(app, auth)
    return app


def _advertise_scopes(app: Starlette, auth: AuthSettings) -> None:
    """Replace the SDK's protected-resource metadata with one that lists
    `scopes_supported`. The SDK fills that field from `required_scopes`, which
    stays None because each tool checks its own scope; without the field a
    client that follows the MCP spec asks /authorize for no scope at all, and
    its token can call nothing."""
    ours = create_protected_resource_routes(
        resource_url=auth.resource_server_url,
        authorization_servers=[auth.issuer_url],
        scopes_supported=sorted(OAUTH_SCOPES),
    )
    paths = {route.path for route in ours}
    app.router.routes[:] = ours + [
        route
        for route in app.router.routes
        if getattr(route, "path", None) not in paths
    ]


def create_app() -> Starlette:
    return build_app(Settings.from_env(), httpx.AsyncClient(timeout=5.0))
