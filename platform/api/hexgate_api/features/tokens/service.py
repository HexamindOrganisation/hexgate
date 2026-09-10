"""API-key persistence: mint (Biscuit-signed), list, revoke, lookup, mask.

Revocation is a soft delete: :func:`revoke_api_key` stamps ``revoked_at`` and
the row stays as the audit record. Every read below therefore filters on
``revoked_at IS NULL`` -- without that, revocation would silently stop working
on every bearer surface (see :func:`find_token_by_secret`).
"""

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.biscuits import MintRequest, make_envelope, mint_token
from hexgate_api.core.ids import new_id
from hexgate_api.models import ApiKey, utcnow


async def mint_api_key(
    session: AsyncSession,
    project_id: str,
    name: str,
    scopes: list[str],
    env: str,
    *,
    signing_key_bytes: bytes,
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
    if token is None or token.project_id != project_id or token.revoked_at is not None:
        return False
    token.revoked_at = utcnow()
    token.revoked_by_user_id = revoked_by_user_id
    session.add(token)
    await session.commit()
    return True


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
