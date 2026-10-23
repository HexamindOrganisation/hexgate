"""The routes the dashboard's consent page calls, as the logged-in user.

Both check the request again as ``/authorize`` did: the page's copy of it is
in the user's browser, so it may have been edited.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from mcp.server.auth.provider import AuthorizeError, construct_redirect_uri
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import OAUTH_SCOPE_DESCRIPTIONS
from hexgate_api.core.db import get_session
from hexgate_api.deps.identity import require_user
from hexgate_api.features.oauth.authorization import CheckedAuthorization
from hexgate_api.features.oauth.grants import issue_code
from hexgate_api.features.oauth.provider import HexgateOAuthProvider
from hexgate_api.features.oauth.schemas import (
    ConsentInfo,
    ConsentProject,
    ConsentProjects,
    ConsentRedirect,
    ConsentScope,
)
from hexgate_api.models import Organization, OrganizationMember, Project, User
from hexgate_api.settings import get_settings

router = APIRouter(prefix="/oauth", tags=["oauth"])


def _refuse_authorization_header(
    authorization: str | None = Header(default=None),
) -> None:
    """Consent is given by a person in the dashboard, never by a token: an OAuth
    token or SDK key must not be able to grant another."""
    if authorization is not None:
        raise HTTPException(status_code=401, detail="consent needs a session cookie")


def _origin(url: str) -> str | None:
    """``url``'s origin as a browser serialises it: lowercase, no default port."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    scheme = parts.scheme.lower()
    if port is None or (scheme, port) in {("http", 80), ("https", 443)}:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _require_dashboard_origin(origin: str | None = Header(default=None)) -> None:
    """Refuse a consent POST sent from any page but the dashboard's."""
    expected = _origin(get_settings().dashboard_url)
    # None on both sides (a malformed setting, Origin: null) is not a match.
    if expected is None or origin is None or _origin(origin) != expected:
        raise HTTPException(
            status_code=403, detail="consent must come from the dashboard"
        )


def _require_json(content_type: str | None = Header(default=None)) -> None:
    """Refuse a body that isn't JSON: a cross-site form can't send JSON without
    a CORS preflight, which the app's CORS policy answers."""
    media_type = (content_type or "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(status_code=400, detail="body must be application/json")


def _provider(request: Request) -> HexgateOAuthProvider:
    return request.app.state.oauth_provider


async def _checked(
    params: Mapping[str, Any], provider: HexgateOAuthProvider
) -> CheckedAuthorization:
    try:
        return await provider.check_request(params)
    except AuthorizeError as exc:
        raise HTTPException(
            status_code=400, detail=f"{exc.error}: {exc.error_description}"
        ) from None


async def _user_projects(
    session: AsyncSession, user: User
) -> list[tuple[Project, Organization]]:
    """Every project in an org the user is a member of."""
    rows = await session.exec(
        select(Project, Organization)
        .join(Organization, Organization.id == Project.org_id)
        .join(OrganizationMember, OrganizationMember.org_id == Project.org_id)
        .where(OrganizationMember.user_id == user.id)
        .order_by(Organization.name, Project.name)
    )
    return list(rows.all())


@router.post(
    "/authorize",
    dependencies=[
        Depends(_refuse_authorization_header),
        Depends(_require_dashboard_origin),
        Depends(_require_json),
    ],
)
async def api_consent(
    request: Request,
    response: Response,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    provider: HexgateOAuthProvider = Depends(_provider),
) -> ConsentRedirect:
    """Record the user's consent: write a one-time code for the checked request
    and the ticked projects, and return the client's redirect carrying it."""
    try:
        body = await request.json()
        projects = ConsentProjects.model_validate(body).projects
    except ValueError:  # pydantic's ValidationError is one too
        raise HTTPException(
            status_code=400, detail="body must be a JSON object with projects"
        ) from None
    checked = await _checked(body, provider)
    projects = list(dict.fromkeys(projects))
    reachable = {project.id for project, _ in await _user_projects(session, user)}
    if not set(projects) <= reachable:
        raise HTTPException(
            status_code=400, detail="projects must be in your organizations"
        )
    code = await issue_code(
        session, user_id=user.id, request=checked, projects=projects
    )
    response.headers["Cache-Control"] = "no-store"  # it carries a live code
    return ConsentRedirect(
        redirect_to=construct_redirect_uri(
            str(checked.redirect_uri), code=code, state=checked.state
        )
    )


@router.get("/consent-info", dependencies=[Depends(_refuse_authorization_header)])
async def api_consent_info(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    provider: HexgateOAuthProvider = Depends(_provider),
) -> ConsentInfo:
    """What the consent page shows for the authorization request in the query."""
    checked = await _checked(request.query_params, provider)
    return ConsentInfo(
        client_name=checked.client.client_name,
        client_host=urlsplit(checked.client.client_id).hostname or "",
        # A private-use scheme (com.example.app:/cb) has no host: show it whole.
        redirect_host=checked.redirect_uri.host or str(checked.redirect_uri),
        scopes=[
            ConsentScope(name=scope, description=OAUTH_SCOPE_DESCRIPTIONS[scope])
            for scope in checked.scopes
        ],
        projects=[
            ConsentProject(
                id=project.id, name=project.name, org_id=org.id, org_name=org.name
            )
            for project, org in await _user_projects(session, user)
        ],
    )
