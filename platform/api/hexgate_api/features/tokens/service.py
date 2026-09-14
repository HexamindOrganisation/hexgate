"""API-key persistence: mint (Biscuit-signed), list, revoke, lookup, mask.

Revocation is a soft delete: :func:`revoke_api_key` stamps ``revoked_at``, masks
the secret, and the row stays as the audit record. Every read below therefore
filters on ``revoked_at IS NULL`` -- without that, revocation would silently stop
working on every bearer surface (see :func:`find_token_by_secret`).

A key carries two actors: ``created_by_user_id`` minted it, ``owner_user_id``
owns it. They differ when an admin mints for a teammate, and the owner is what
:func:`revoke_owned_keys` sweeps when that teammate leaves.
"""

from sqlmodel import select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import ROLE_ADMIN, ROLE_OWNER
from hexgate_api.core.biscuits import MintRequest, make_envelope, mint_token
from hexgate_api.core.ids import new_id
from hexgate_api.models import ApiKey, Project, User, utcnow


class MintDelegationError(Exception):
    """The caller may not mint a key owned by this user. Routes -> 403."""


async def resolve_mint_owner(
    session: AsyncSession,
    *,
    project_id: str,
    caller: User,
    requested_owner_user_id: str | None,
) -> str:
    """Decide whose key a mint creates, refusing a delegation the caller can't make.

    No owner requested, or your own id: not delegation, so any member passes.
    Minting for someone else needs both (else :class:`MintDelegationError`):

      * the caller is ``owner``/``admin`` of the project's org. Checked here,
        not in the dep — the route must stay open to plain members minting for
        themselves.
      * the requested owner is a member of that org, or the key would be parked
        beyond the reach of the offboarding sweep.
    """
    if requested_owner_user_id is None or requested_owner_user_id == caller.id:
        return caller.id

    from hexgate_api.features.members.service import find_member

    project = await session.get(Project, project_id)
    # require_org_member has already 404'd an unknown project before this runs.
    assert project is not None, "resolve_mint_owner called for an unknown project"

    caller_member = await find_member(session, org_id=project.org_id, user_id=caller.id)
    if caller_member is None or caller_member.role not in {ROLE_OWNER, ROLE_ADMIN}:
        raise MintDelegationError(
            "only admins / owners can mint an API key for another member"
        )
    if (
        await find_member(
            session, org_id=project.org_id, user_id=requested_owner_user_id
        )
        is None
    ):
        raise MintDelegationError(
            "the requested owner is not a member of this project's organization"
        )
    return requested_owner_user_id


async def mint_api_key(
    session: AsyncSession,
    project_id: str,
    name: str,
    scopes: list[str],
    env: str,
    *,
    signing_key_bytes: bytes,
    created_by_user_id: str | None = None,
    owner_user_id: str | None = None,
) -> tuple[ApiKey, str]:
    """Create a new API key, signed as a Biscuit by the platform's root key.

    The wire format stays human-readable: ``fty_<env>_<project>_<biscuit_b64>``.
    Project id is duplicated in the prefix (for grep / GitHub-secret-scanning)
    and inside the Biscuit's claims (the source of truth at verification time).

    ``signing_key_bytes`` are the raw 32-byte Ed25519 private key from the
    platform's keystore. Pulled out of the keystore at the call site so this
    function stays decoupled from where the key actually lives.

    Returns the persisted row + the full token string (the b64 form is what
    the operator copies out of the dashboard — shown once, never stored
    in the row outside of the ``secret`` column for revocation lookup).

    Both actors default to ``None`` for system mints (``deploy/provision.py``)
    and test helpers; the mint route always passes both. A NULL owner is
    invisible to :func:`revoke_owned_keys`: it belongs to no one, so no
    departure should kill it.
    """
    # Same id for the row's primary key and for the token_id fact signed into
    # the biscuit below. The OTLP Collector looks a token up in its cache by
    # that fact, so it has to be the row id.
    token_id = new_id(ApiKey)
    biscuit_b64 = mint_token(
        signing_key_bytes,
        MintRequest(
            project_id=project_id,
            token_id=token_id,
            name=name,
            scopes=scopes,
            env=env,
            ttl_seconds=None,  # API keys don't expire by default; revoke explicitly.
        ),
    )
    prefix = f"fty_{env}"
    full_token = make_envelope(env, project_id, biscuit_b64)

    token = ApiKey(
        id=token_id,
        project_id=project_id,
        name=name,
        prefix=prefix,
        secret=full_token,
        scopes_csv=",".join(scopes),
        created_by_user_id=created_by_user_id,
        owner_user_id=owner_user_id,
    )
    session.add(token)
    await session.commit()
    await session.refresh(token)
    return token, full_token


async def list_api_keys(session: AsyncSession, project_id: str) -> list[ApiKey]:
    """Live keys only. Revoked rows are retained as the audit trail (see
    :class:`ApiKey`) but never surface in the dashboard's key list."""
    stmt = (
        select(ApiKey)
        .where(
            ApiKey.project_id == project_id,
            ApiKey.revoked_at.is_(None),  # type: ignore[union-attr]
        )
        .order_by(ApiKey.created_at.desc())
    )  # type: ignore[attr-defined]
    return list((await session.exec(stmt)).all())


async def find_token_by_secret(session: AsyncSession, secret: str) -> ApiKey | None:
    """Look up a live token by its full secret value. Updates last_used_at on hit.

    The ``revoked_at`` filter is the revocation gate for every bearer surface:
    ``deps/tokens.py`` (``_validate_sdk_token``, ``require_project``),
    ``deps/ws.py`` (``ws_require_project``) and ``GET /v1/me/key`` all reach
    revocation through here. Filtering also stops a revoked key from bumping
    its own ``last_used_at``.
    """
    stmt = select(ApiKey).where(
        ApiKey.secret == secret,
        ApiKey.revoked_at.is_(None),  # type: ignore[union-attr]
    )
    token = (await session.exec(stmt)).first()
    if token is not None:
        token.last_used_at = utcnow()
        session.add(token)
        await session.commit()
    return token


def _revoke_stmt(token: ApiKey, revoked_by_user_id: str):
    """The conditional UPDATE that soft-revokes one key.

    ``revoked_at IS NULL`` is in the WHERE clause, not a check on a prior read:
    two concurrent revokes would both clear a read-then-check and the second
    would overwrite the first's stamp. ``rowcount`` says whether this call was
    the one that revoked.

    ``token`` is read only to mask its secret -- an audit row must not stay a
    usable credential. Shared by both revoke paths so neither can drift.
    """
    return (
        update(ApiKey)
        .where(
            ApiKey.id == token.id,
            ApiKey.revoked_at.is_(None),  # type: ignore[union-attr]
        )
        .values(
            revoked_at=utcnow(),
            revoked_by_user_id=revoked_by_user_id,
            secret=mask_secret(token.secret),
        )
    )


async def revoke_api_key(
    session: AsyncSession,
    project_id: str,
    token_id: str,
    *,
    revoked_by_user_id: str,
) -> bool:
    """Soft-delete a key, project-scoped.

    False if unknown here or already revoked -- the router turns that into the
    404 a repeat revoke has always returned, and a second call never moves the
    timestamp. The three cases share one guard because they share one response:
    distinguishing them here invites distinguishing them in the reply, which
    would leak whether a token id exists.
    """
    token = await session.get(ApiKey, token_id)
    if token is None or token.project_id != project_id:
        return False
    result = await session.exec(_revoke_stmt(token, revoked_by_user_id))
    await session.commit()
    return result.rowcount == 1


async def revoke_owned_keys(
    session: AsyncSession,
    *,
    org_id: str,
    owner_user_id: str,
    revoked_by_user_id: str,
) -> int:
    """Soft-revoke every live key owned by ``owner_user_id`` across ``org_id``'s
    projects. Returns how many this call actually revoked.

    Stamps in the caller's transaction and never commits, so a removal later
    refused (``LastOwnerError``) doesn't kill the keys of someone who stays.

    Keys with no recorded owner are untouched: they belong to a system path,
    not to whoever just left.
    """
    stmt = (
        select(ApiKey)
        .join(Project, Project.id == ApiKey.project_id)  # type: ignore[arg-type]
        .where(
            Project.org_id == org_id,
            ApiKey.owner_user_id == owner_user_id,
            ApiKey.revoked_at.is_(None),  # type: ignore[union-attr]
        )
    )
    revoked = 0
    for token in (await session.exec(stmt)).all():
        result = await session.exec(_revoke_stmt(token, revoked_by_user_id))
        revoked += result.rowcount
    return revoked


def mask_secret(full: str) -> str:
    """Return e.g. ``fty_live_8F3d…k29P`` for list display.

    Skips trailing ``=`` base64 padding when computing the tail so masked
    Biscuit envelopes don't end on a meaningless ``=`` character.
    """
    if len(full) <= 16:
        return full
    head = full[:12]
    body = full.rstrip("=")
    tail = body[-4:] if len(body) >= 4 else body
    return f"{head}…{tail}"
