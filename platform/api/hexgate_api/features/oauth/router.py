"""The routes the dashboard's consent page calls, as the logged-in user.

Each checks the request again as ``/authorize`` did: the page's copy of it is
in the user's browser, so it may have been edited.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from mcp.server.auth.provider import AuthorizeError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import OAUTH_SCOPE_DESCRIPTIONS
from hexgate_api.core.db import get_session
from hexgate_api.deps.identity import require_user
from hexgate_api.features.oauth.authorization import CheckedAuthorization
from hexgate_api.features.oauth.provider import HexgateOAuthProvider
from hexgate_api.features.oauth.schemas import (
    ConsentInfo,
    ConsentProject,
    ConsentScope,
)
from hexgate_api.models import Organization, OrganizationMember, Project, User

router = APIRouter(prefix="/oauth", tags=["oauth"])


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
