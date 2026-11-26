"""What the policy assistant may do for one user on one project, and its token."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import ASSISTANT_CLIENT_ID, ROLE_ADMIN, ROLE_OWNER
from hexgate_api.core.oauth_tokens import ACCESS_TOKEN_TTL_SECONDS, mint_access_token
from hexgate_api.models import Organization, OrganizationMember, Project
from hexgate_api.schemas import AssistantMode
from hexgate_api.settings import get_settings

# Every assistant token reads what the assistant's tools read. Never
# policy:write, which reaches the file PUT that runs no checks, and never
# projects:read, which none of the assistant's tools need.
READ_SCOPES = ("policy:read", "audit:read", "agents:read")
APPLY_SCOPE = "policy:apply"


@dataclass(frozen=True)
class AssistantAccess:
    # The stored column, unvalidated: the token route refuses any other value.
    mode: AssistantMode
    # Admin or owner, in an org whose mode is read_write.
    can_write: bool
    # The project's org, which the assistant's token names.
    org_id: str


async def assistant_access(
    session: AsyncSession, project_id: str, user_id: str
) -> AssistantAccess:
    """The org's assistant mode and whether this user may write, read now:
    a role or mode change applies to the next token."""
    mode, role, org_id = (
        await session.exec(
            select(
                Organization.assistant_mode,
                OrganizationMember.role,
                Project.org_id,
            )
            .join(Project, Project.org_id == Organization.id)
            .join(OrganizationMember, OrganizationMember.org_id == Organization.id)
            .where(Project.id == project_id, OrganizationMember.user_id == user_id)
        )
    ).one()  # require_org_member has found this membership
    can_write = role in {ROLE_OWNER, ROLE_ADMIN} and mode == "read_write"
    return AssistantAccess(mode=mode, can_write=can_write, org_id=org_id)


def mint_assistant_token(
    *, user_id: str, project_id: str, org_id: str, can_write: bool
) -> tuple[str, datetime]:
    """A ten-minute token for one user and one project, and when it expires:
    for the assistant, then the MCP server, then the API, each pinned to its
    own audience."""
    settings = get_settings()
    scopes = [*READ_SCOPES, APPLY_SCOPE] if can_write else list(READ_SCOPES)
    now = int(time.time())
    token = mint_access_token(
        sub=user_id,
        client_id=ASSISTANT_CLIENT_ID,
        scopes=scopes,
        projects=[project_id],
        issuer=settings.public_url,
        audience=[
            settings.assistant_resource_url,
            settings.mcp_resource_url,
            settings.public_url,
        ],
        now=now,
        org=org_id,
    )
    return token, datetime.fromtimestamp(now + ACCESS_TOKEN_TTL_SECONDS, UTC)
