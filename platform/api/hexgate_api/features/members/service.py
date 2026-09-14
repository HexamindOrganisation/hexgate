"""Org membership persistence + the role-rank rules shared with invitations.

The "at least one owner" invariant and the at-or-below role-escalation rule
live here so every caller (PATCH member role, accept invite) respects them.

Removing a member also revokes the API keys they own (:func:`remove_member`):
keys never expire, so otherwise a leaver's credentials outlive their access.
"""

from dataclasses import dataclass

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import ALL_ROLES, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER
from hexgate_api.features.tokens.service import revoke_owned_keys
from hexgate_api.models import OrganizationMember, User, utcnow


async def emails_for_user_ids(
    session: AsyncSession, user_ids: set[str]
) -> dict[str, str]:
    """Map user id -> email for the given ids in one query. Ids with no live
    User row are omitted so callers fall back to the id.

    Lives here rather than in bans: three slices now resolve actor ids.
    """
    ids = {uid for uid in user_ids if uid}
    if not ids:
        return {}
    rows = await session.exec(select(User.id, User.email).where(User.id.in_(ids)))  # type: ignore[attr-defined]
    return {uid: email for uid, email in rows.all()}


async def find_member(
    session: AsyncSession, *, org_id: str, user_id: str
) -> OrganizationMember | None:
    """Return the OrganizationMember row for (org, user), or None."""
    stmt = select(OrganizationMember).where(
        OrganizationMember.org_id == org_id,
        OrganizationMember.user_id == user_id,
    )
    return (await session.exec(stmt)).first()


async def list_org_members(
    session: AsyncSession, org_id: str
) -> list[tuple[OrganizationMember, User]]:
    """Return (membership, user) tuples for an org's members."""
    stmt = (
        select(OrganizationMember, User)
        .join(User, User.id == OrganizationMember.user_id)
        .where(OrganizationMember.org_id == org_id)
        .order_by(OrganizationMember.created_at)  # type: ignore[attr-defined]
    )
    return [(m, u) for m, u in (await session.exec(stmt)).all()]


async def _count_owners(session: AsyncSession, org_id: str) -> int:
    """How many ROLE_OWNER members an org currently has."""
    stmt = select(OrganizationMember).where(
        OrganizationMember.org_id == org_id,
        OrganizationMember.role == ROLE_OWNER,
    )
    return len((await session.exec(stmt)).all())


class LastOwnerError(Exception):
    """Raised when an action would leave an org with zero owners.

    Service-layer business-rule signal — routes translate to HTTP 409.
    """


@dataclass(frozen=True)
class MemberRemoval:
    """Whether the membership went, and how many of their keys went with it.

    An object, so ``if result:`` is always truthy — test ``result.removed``.
    """

    removed: bool
    revoked_key_count: int


async def remove_member(
    session: AsyncSession, *, org_id: str, user_id: str, removed_by_user_id: str
) -> MemberRemoval:
    """Remove (user, org) membership **and revoke the API keys they own**.

    Refuses with :class:`LastOwnerError` if the removal would leave the org
    with zero owners; ``removed=False`` when the membership didn't exist.

    Order and transaction scope are load-bearing: the last-owner guard runs
    first, so a refused removal revokes nothing, and the sweep stamps without
    committing so both halves land in one commit. Half an offboarding is worse
    than either outcome alone.
    """
    member = await find_member(session, org_id=org_id, user_id=user_id)
    if member is None:
        return MemberRemoval(removed=False, revoked_key_count=0)
    if member.role == ROLE_OWNER and await _count_owners(session, org_id) <= 1:
        raise LastOwnerError(
            "cannot remove the last owner; promote another member to owner first"
        )
    revoked = await revoke_owned_keys(
        session,
        org_id=org_id,
        owner_user_id=user_id,
        revoked_by_user_id=removed_by_user_id,
    )
    await session.delete(member)
    await session.commit()
    return MemberRemoval(removed=True, revoked_key_count=revoked)


class RoleEscalationError(PermissionError):
    """Raised when a caller tries to set a member role above their own.

    Mirrors the :func:`_can_invite_role` rank check so the
    PATCH-member-role surface stays consistent with the invitation
    surface. Without this guard, an admin could PATCH their own
    membership row to ``{"role": "owner"}`` and seize the org —
    bypassing every other gate this layer enforces.
    """


# Role hierarchy as integers — higher is more privileged. Shared by
# :func:`_can_invite_role` and the accept-invite upgrade path so a
# refactor of one keeps both branches consistent.
_ROLE_RANK: dict[str, int] = {ROLE_MEMBER: 0, ROLE_ADMIN: 1, ROLE_OWNER: 2}


def _can_invite_role(inviter_role: str, target_role: str) -> bool:
    """True if a member with ``inviter_role`` can mint an invite for
    ``target_role``.

    Rule: at-or-below. Owners can invite anyone; admins can invite
    admin + member; members can't invite (the route layer rejects them
    upstream via require_org_admin). The rule stops privilege
    escalation by-design — admins can't mint owner invites and use them
    to promote themselves.
    """
    return _ROLE_RANK.get(inviter_role, -1) >= _ROLE_RANK.get(target_role, 99)


async def change_member_role(
    session: AsyncSession,
    *,
    org_id: str,
    user_id: str,
    new_role: str,
    caller_role: str,
    updated_by_user_id: str,
) -> OrganizationMember | None:
    """Update a member's role. Returns the updated row, or None when
    the membership doesn't exist.

    Two refusal gates:
      * :class:`RoleEscalationError` — the caller can't assign a role
        above their own rank. Owner can set anything; admin can set
        admin + member; member can't reach this code path (the route
        layer rejects them via ``require_org_admin``).
      * :class:`LastOwnerError` — demoting the only owner is refused.

    ``caller_role`` is the caller's role on this org (resolved by the
    route layer via :func:`require_org_admin`); ``updated_by_user_id`` is that
    same caller.
    """
    if new_role not in ALL_ROLES:
        raise ValueError(f"unknown role: {new_role!r}")
    if not _can_invite_role(caller_role, new_role):
        raise RoleEscalationError(
            f"{caller_role} cannot assign role {new_role!r} — "
            "callers can only set roles at or below their own rank"
        )
    member = await find_member(session, org_id=org_id, user_id=user_id)
    if member is None:
        return None
    demoting_owner = member.role == ROLE_OWNER and new_role != ROLE_OWNER
    if demoting_owner and await _count_owners(session, org_id) <= 1:
        raise LastOwnerError(
            "cannot demote the last owner; promote another member to owner first"
        )
    member.role = new_role
    member.updated_at = utcnow()
    member.updated_by_user_id = updated_by_user_id
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member
