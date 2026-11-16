"""The routes the dashboard calls, as the logged-in user: the consent page's
two, and the user's list of live logins.

The consent routes check the request again as ``/authorize`` did: the page's
copy of it is in the user's browser, so it may have been edited.
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
from hexgate_api.features.oauth.grants import (
    as_utc,
    issue_code,
    live_grants,
    projects_of,
    revoke_family,
    scopes_of,
)
from hexgate_api.features.oauth.provider import HexgateOAuthProvider
from hexgate_api.features.oauth.schemas import (
    ConsentInfo,
    ConsentProject,
    ConsentProjects,
    ConsentRedirect,
    ConsentScope,
    Grant,
)
from hexgate_api.models import Organization, OrganizationMember, Project, User
from hexgate_api.settings import get_settings

router = APIRouter(prefix="/oauth", tags=["oauth"])


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


_PROJECTS = select(Project, Organization).join(
    Organization, Organization.id == Project.org_id
)


def _consent_project(project: Project, org: Organization) -> ConsentProject:
    return ConsentProject(
        id=project.id, name=project.name, org_id=org.id, org_name=org.name
    )


async def _user_projects(
    session: AsyncSession, user: User
) -> list[tuple[Project, Organization]]:
    """Every project in an org the user is a member of."""
    rows = await session.exec(
        _PROJECTS.join(OrganizationMember, OrganizationMember.org_id == Project.org_id)
        .where(OrganizationMember.user_id == user.id)
        .order_by(Organization.name, Project.name)
    )
    return list(rows.all())


# Unmarked, like consent-info: a token never gives a consent.
@router.post(
    "/authorize",
    # require_user first, so who is calling is decided before the request's
    # shape (FastAPI runs it once; the handler's user is the cached result).
    dependencies=[
        Depends(require_user),
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


# Unmarked, so require_user's default-deny refuses any OAuth token (403): only
# a person in the dashboard, with a session cookie, sees or gives a consent.
@router.get("/consent-info")
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
        scopes=_scopes(checked.scopes),
        projects=[
            _consent_project(project, org)
            for project, org in await _user_projects(session, user)
        ],
    )


def _scopes(names: list[str]) -> list[ConsentScope]:
    return [
        # A grant's scopes were checked at consent; one since removed from the
        # registry is shown by name rather than failing the whole list.
        ConsentScope(name=name, description=OAUTH_SCOPE_DESCRIPTIONS.get(name, name))
        for name in names
    ]


async def _projects_by_id(
    session: AsyncSession, ids: set[str]
) -> dict[str, ConsentProject]:
    rows = await session.exec(_PROJECTS.where(Project.id.in_(ids)))
    return {project.id: _consent_project(project, org) for project, org in rows.all()}


# Unmarked, like the consent routes: only the user, with a session cookie,
# sees or revokes their grants.
@router.get("/grants")
async def api_list_grants(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[Grant]:
    """The user's live logins, newest first."""
    rows = await live_grants(session, user.id)
    if not rows:
        return []
    projects = await _projects_by_id(
        session, {pid for row in rows for pid in projects_of(row)}
    )
    return [
        Grant(
            id=row.family_id,
            client_name=row.client_name,
            client_id=row.client_id,
            scopes=_scopes(scopes_of(row)),
            projects=[projects[pid] for pid in projects_of(row) if pid in projects],
            # Aware, so the page doesn't read SQLite's naive values as local time.
            created_at=as_utc(row.created_at),
            last_used_at=row.last_used_at and as_utc(row.last_used_at),
        )
        for row in rows
    ]


@router.delete("/grants/{grant_id}", status_code=204)
async def api_revoke_grant(
    grant_id: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Log the client out: revoke the login's live token, so its next refresh
    fails. Access tokens already issued stay valid until they expire, within
    ``ACCESS_TOKEN_TTL_SECONDS``. 404 for another user's grant, so a guessed id
    tells nothing about who owns it."""
    if not await revoke_family(session, grant_id, user_id=user.id):
        raise HTTPException(status_code=404, detail="grant not found")
