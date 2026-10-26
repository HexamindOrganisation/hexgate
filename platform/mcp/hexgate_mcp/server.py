"""The FastMCP app: streamable HTTP at /mcp behind the JWKS token verifier."""

from urllib.parse import urlparse

import httpx
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.auth.routes import build_resource_metadata_url, cors_middleware
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import ProtectedResourceMetadata
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from hexgate_mcp.api import ApiClient
from hexgate_mcp.auth import JwksTokenVerifier
from hexgate_mcp.jwks import JwksCache
from hexgate_mcp.scopes import OAUTH_SCOPES
from hexgate_mcp.settings import Settings
from hexgate_mcp.tools import (
    agents,
    audit,
    policy,
    policy_save,
    projects,
    reference,
    write_local,
)


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
        instructions=reference.INSTRUCTIONS,
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
    reference.register(mcp)
    api = ApiClient(settings.api_v1_url, http)
    for tools in (projects, policy, policy_save, audit, agents):
        tools.register(mcp, api)
    if settings.local_policy_dir is not None:
        write_local.register(mcp, settings.local_policy_dir)
    app = mcp.streamable_http_app()
    _serve_resource_metadata(app, auth, settings.issuer)
    _refuse_get_stream(app, auth, mcp.settings.streamable_http_path)
    return app


def _refuse_get_stream(app: Starlette, auth: AuthSettings, path: str) -> None:
    """Answer an authenticated GET on the MCP path with 405. A stateless server
    never pushes a message outside a POST's own response, so the SSE stream a
    client opens with GET would only carry pings until the connection times
    out; the MCP spec lets the server refuse it, and clients then stop asking.
    Behind the same auth check as the SDK's route, so a GET with no token still
    gets the 401 that names the resource metadata. Inserted ahead of the SDK's
    route, which accepts every method."""
    endpoint = RequireAuthMiddleware(
        Response(status_code=405, headers={"Allow": "POST"}),
        required_scopes=[],
        resource_metadata_url=build_resource_metadata_url(auth.resource_server_url),
    )
    app.router.routes.insert(0, Route(path, endpoint, methods=["GET"]))


def _serve_resource_metadata(app: Starlette, auth: AuthSettings, issuer: str) -> None:
    """Replace the SDK's protected-resource metadata with our own document.
    The SDK fills `scopes_supported` from `required_scopes`, which stays None
    because each tool checks its own scope; without the field a client that
    follows the MCP spec asks /authorize for no scope at all, and its token can
    call nothing. The SDK also types `authorization_servers` as AnyHttpUrl,
    which serialises the issuer with a trailing slash, so it no longer equals
    the API's metadata `issuer` and an RFC 8414 §3.3 check fails. The SDK
    model still builds the rest of the document, so its defaults carry over."""
    document = ProtectedResourceMetadata(
        resource=auth.resource_server_url,
        authorization_servers=[issuer],
        scopes_supported=sorted(OAUTH_SCOPES),
    ).model_dump(mode="json", exclude_none=True) | {"authorization_servers": [issuer]}

    async def metadata(_: Request) -> JSONResponse:
        return JSONResponse(document, headers={"Cache-Control": "public, max-age=3600"})

    url = build_resource_metadata_url(auth.resource_server_url)
    path = urlparse(str(url)).path
    route = Route(
        path,
        endpoint=cors_middleware(metadata, ["GET", "OPTIONS"]),
        methods=["GET", "OPTIONS"],
    )
    app.router.routes[:] = [route] + [
        r for r in app.router.routes if getattr(r, "path", None) != path
    ]


def create_app(settings: Settings) -> Starlette:
    return build_app(settings, httpx.AsyncClient(timeout=5.0))
