"""Bearer/SDK-token dependencies — machine auth via ``Authorization: Bearer``.

The signing keystore is read lazily from :mod:`hexgate_api.core.keystore` at
call time so the singleton stays in one place and test swaps of
``core.keystore.keystore`` are picked up.
"""

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.biscuits import (
    TokenError,
    TokenSignatureError,
    parse_envelope,
    verify_token,
)
from hexgate_api.core.db import get_session
from hexgate_api.features.tokens.service import find_token_by_secret
from hexgate_api.models import ApiKey


@dataclass(frozen=True)
class TokenActor:
    """The (project, human) pair behind a bearer-authenticated write.

    ``user_id`` is the key's recorded owner (``ApiKey.owner_user_id``) and is
    ``None`` when the key has none — minted before issue #160, or by a system
    path such as ``deploy/provision.py``. The actor columns are nullable for
    exactly this case: a bearer write with no owner records NULL, not a guess.
    """

    project_id: str
    user_id: str | None


async def _validate_sdk_token(authorization: str, session: AsyncSession) -> ApiKey:
    """Validate an ``Authorization: Bearer <hexgate_key>`` biscuit envelope.

    Used by :func:`optional_api_key` (allows a missing header) and
    indirectly by :func:`require_project` / :func:`require_project_actor` /
    :func:`ws_require_project` (the bearer-implicit SDK routes). Raises 401 on
    signature or revocation failure; returns the live row on success, so
    callers needing the project or the key's owner don't look it up twice.
    """
    from hexgate_api.core.keystore import keystore

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="malformed authorization header")
    secret = authorization.removeprefix("Bearer ").strip()

    # Signature gate
    try:
        _, _, biscuit_b64 = parse_envelope(secret)
    except TokenError:
        raise HTTPException(status_code=401, detail="malformed hexgate key") from None
    try:
        verify_token(biscuit_b64, keystore.public_key_bytes())
    except TokenSignatureError:
        raise HTTPException(
            status_code=401, detail="invalid hexgate key signature"
        ) from None

    # Revocation gate
    token = await find_token_by_secret(session, secret)
    if token is None:
        raise HTTPException(status_code=401, detail="unknown or revoked hexgate key")
    return token


async def _resolve_bearer(authorization: str | None, session: AsyncSession) -> ApiKey:
    """``Authorization`` header → the live :class:`ApiKey` row behind it.

    The shared body of :func:`require_project` and :func:`require_project_actor`
    so the two bearer deps can never disagree about what counts as a valid
    token.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="missing or malformed authorization header"
        )
    return await _validate_sdk_token(authorization, session)


async def optional_api_key(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Validate Authorization: Bearer <hexgate_key> when present.

    Two gates run when a header is supplied:

    1. **Signature verification** — parse the envelope, decode the Biscuit,
       check it chains to the platform's root public key.
    2. **Revocation lookup** — confirm the exact secret is still in the
       ``ApiKey`` table and update ``last_used_at``.

    POC behaviour: the header itself remains optional so the dashboard
    (no user-session concept yet) can keep calling these endpoints
    unauthenticated. Routes that DO require some auth pick the
    appropriate dep:

      * cookie/dashboard humans → :func:`require_org_member`
      * bearer/SDK machines → :func:`require_project` (HTTP) or
        :func:`ws_require_project` (WebSocket)
    """
    if authorization is None:
        return
    await _validate_sdk_token(authorization, session)


async def require_project(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> str:
    """Resolve `Authorization: Bearer <hexgate_key>` to a project_id.

    Used by SDK-facing endpoints where the caller has only an API key, not a
    project id in the URL. Routes that also need to attribute a *write* take
    :func:`require_project_actor` instead.

    Two gates run in order, matching :func:`ws_require_project` and
    :func:`optional_api_key` so all three bearer-auth surfaces agree
    on what counts as a valid token:

      1. **Signature verification** via :func:`_validate_sdk_token` —
         parse the envelope, verify the biscuit chains to the platform's
         root public key. A revocation lookup runs inside the helper.
      2. **Project resolution** — read ``ApiKey.project_id`` off the row that
         helper returns.

    The signature gate was missing before — a forged biscuit whose
    secret string happened to match a stored ``ApiKey.secret`` would
    have been accepted. Defense-in-depth + consistency with the WS
    bearer path.
    """
    return (await _resolve_bearer(authorization, session)).project_id


async def require_project_actor(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> TokenActor:
    """Like :func:`require_project`, plus the human behind the key.

    The bearer half of the control-plane actor trail (issue #160): a
    token-authenticated write (``POST /v1/agents``) has no cookie session, so
    ``ApiKey.owner_user_id`` is the only bridge from a credential back to a
    person. Same two gates, same 401s — this dep adds no authority, only
    attribution.

    ``TokenActor.user_id`` is ``None`` for a key with no recorded owner. That is
    the intended value, not a gap to paper over with a sentinel: the actor
    columns are nullable FKs to ``user`` precisely so an unattributable write
    records nothing rather than something false.
    """
    token = await _resolve_bearer(authorization, session)
    return TokenActor(project_id=token.project_id, user_id=token.owner_user_id)
