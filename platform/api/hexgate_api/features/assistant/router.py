"""The dashboard's routes for its policy assistant: the per-message token and
the tab's config.

Both are unmarked, so ``require_user`` refuses any OAuth token (403): only a
person in the dashboard, with a session cookie, gets an assistant token. An
MCP client could otherwise mint itself one without the checks below.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.db import get_session
from hexgate_api.deps.org import require_org_member
from hexgate_api.deps.origin import require_dashboard_origin
from hexgate_api.features.assistant.service import (
    assistant_access,
    mint_assistant_token,
)
from hexgate_api.models import User
from hexgate_api.schemas import AssistantConfig, AssistantToken
from hexgate_api.settings import get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/assistant", tags=["assistant"])


@router.post(
    "/token",
    # require_org_member first, so who is calling is decided before the Origin.
    dependencies=[Depends(require_org_member), Depends(require_dashboard_origin)],
)
async def api_assistant_token(
    project_id: str,
    response: Response,
    user: User = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> AssistantToken:
    """A token for the assistant's next message on this project."""
    if not get_settings().assistant_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    if not user.is_verified:
        raise HTTPException(status_code=403, detail={"code": "not_verified"})
    access = await assistant_access(session, project_id, user.id)
    if access.mode == "off":
        raise HTTPException(status_code=403, detail={"code": "assistant_off"})
    token, expires_at = mint_assistant_token(
        user_id=user.id, project_id=project_id, can_write=access.can_write
    )
    # Who and where only: the token itself is a bearer credential.
    _log.info(
        "assistant token minted: sub=%s project_id=%s can_write=%s",
        user.id,
        project_id,
        access.can_write,
    )
    response.headers["Cache-Control"] = "no-store"
    return AssistantToken(
        token=token,
        expires_at=expires_at,
        can_write=access.can_write,
    )


@router.get("/config")
async def api_assistant_config(
    project_id: str,
    user: User = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> AssistantConfig:
    """Whether the dashboard shows the Assistant tab, and what it may do."""
    access = await assistant_access(session, project_id, user.id)
    return AssistantConfig(
        enabled=get_settings().assistant_enabled,
        mode=access.mode,
        can_write=access.can_write,
        verified=user.is_verified,
    )
