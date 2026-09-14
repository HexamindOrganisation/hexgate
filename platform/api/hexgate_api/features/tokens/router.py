"""API-key CRUD, key introspection, and the signing-key JWKS endpoint.

The signing keystore is read lazily from :mod:`hexgate_api.main` at call time
(the platform's shared singleton; see :mod:`hexgate_api.deps.tokens`).
"""

import base64

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.db import get_session
from hexgate_api.deps.org import require_org_member
from hexgate_api.schemas import (
    KeyIntrospection,
    TokenListItem,
    TokenMintRequest,
    TokenMintResponse,
)
from hexgate_api.features.members.service import emails_for_user_ids
from hexgate_api.features.tokens.service import (
    MintDelegationError,
    find_token_by_secret,
    list_api_keys,
    mask_secret,
    mint_api_key,
    resolve_mint_owner,
    revoke_api_key,
)
from hexgate_api.models import User
from hexgate_api.seeds.defaults import ensure_default_project

router = APIRouter()


@router.get("/.well-known/keys")
async def well_known_keys() -> dict[str, object]:
    """Publish the platform's signing public key + fingerprint.

    JWKS-shaped so we can grow into multi-key publishing later without
    breaking clients. Lets dashboards and CLIs sanity-check that what
    their SDK has embedded matches what this platform is signing with.
    """
    from hexgate_api.core.keystore import keystore

    return {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "use": "sig",
                "x": base64.urlsafe_b64encode(keystore.public_key_bytes())
                .rstrip(b"=")
                .decode("ascii"),
                "fingerprint": keystore.fingerprint(),
            }
        ]
    }


@router.get(
    "/projects/{project_id}/tokens",
    response_model=list[TokenListItem],
    dependencies=[Depends(require_org_member)],
)
async def list_tokens(
    project_id: str, session: AsyncSession = Depends(get_session)
) -> list[TokenListItem]:
    """Live keys, with both actors' emails resolved in one extra query.

    Same shape as ``api_list_bans``: the wire carries email and id, so the
    dashboard never looks users up itself and can fall back to the id.
    """
    tokens = await list_api_keys(session, project_id)
    emails = await emails_for_user_ids(
        session,
        {t.created_by_user_id for t in tokens if t.created_by_user_id}
        | {t.owner_user_id for t in tokens if t.owner_user_id},
    )
    return [
        TokenListItem(
            id=t.id,
            name=t.name,
            masked=mask_secret(t.secret),
            scopes=t.scopes_csv.split(",") if t.scopes_csv else [],
            created_at=t.created_at,
            last_used_at=t.last_used_at,
            created_by_user_id=t.created_by_user_id,
            created_by_email=emails.get(t.created_by_user_id or ""),
            owner_user_id=t.owner_user_id,
            owner_email=emails.get(t.owner_user_id or ""),
        )
        for t in tokens
    ]


@router.post(
    "/projects/{project_id}/tokens",
    response_model=TokenMintResponse,
    status_code=201,
)
async def mint_token(
    project_id: str,
    body: TokenMintRequest,
    user: User = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> TokenMintResponse:
    """Mint a key, attributed to the caller and owned by ``body.owner_user_id``.

    ``require_org_member`` moves out of the decorator so its ``User`` can be
    stamped as the minter. Delegating ownership is gated in
    ``resolve_mint_owner``.
    """
    from hexgate_api.core.keystore import keystore

    await ensure_default_project(
        session
    )  # POC: lazy-create so single project works out of the box
    try:
        owner_user_id = await resolve_mint_owner(
            session,
            project_id=project_id,
            caller=user,
            requested_owner_user_id=body.owner_user_id,
        )
    except MintDelegationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    token, full = await mint_api_key(
        session,
        project_id=project_id,
        name=body.name,
        scopes=body.scopes,
        env=body.env,
        signing_key_bytes=keystore._private_key_bytes(),
        created_by_user_id=user.id,
        owner_user_id=owner_user_id,
    )
    # The minter is the caller, so only a delegated owner needs a lookup.
    owner_email = user.email
    if owner_user_id != user.id:
        owner_email = (await emails_for_user_ids(session, {owner_user_id})).get(
            owner_user_id
        )
    return TokenMintResponse(
        id=token.id,
        name=token.name,
        full=full,
        masked=mask_secret(full),
        scopes=token.scopes_csv.split(",") if token.scopes_csv else [],
        created_at=token.created_at,
        created_by_user_id=user.id,
        created_by_email=user.email,
        owner_user_id=owner_user_id,
        owner_email=owner_email,
    )


@router.delete(
    "/projects/{project_id}/tokens/{token_id}",
    status_code=204,
)
async def revoke_token(
    project_id: str,
    token_id: str,
    user: User = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Soft-delete the key. ``require_org_member`` moves out of the decorator's
    ``dependencies`` so its ``User`` can be stamped as the revoker."""
    ok = await revoke_api_key(session, project_id, token_id, revoked_by_user_id=user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="token not found")


@router.get("/me/key", response_model=KeyIntrospection)
async def api_introspect_key(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> KeyIntrospection:
    """Describe the bearer token (project + env + scopes).

    Useful for the CLI's startup log line — ``hexgate serve`` can show
    ``project=acme-prod env=live`` without parsing the envelope itself,
    and we keep the parse-envelope contract on one side (the server).

    Authentication is the bearer; possessing the key proves the right
    to read its own description. ``find_token_by_secret`` already bumps
    ``last_used_at`` so this call counts as activity (visible in the
    dashboard's "last used" column).
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="missing or malformed authorization header"
        )
    secret = authorization.removeprefix("Bearer ").strip()
    token = await find_token_by_secret(session, secret)
    if token is None:
        raise HTTPException(status_code=401, detail="invalid hexgate key")

    # ``prefix`` on the row is ``fty_test`` or ``fty_live``; strip the
    # leading ``fty_`` to expose just the env value the CLI cares about.
    env = token.prefix.removeprefix("fty_")
    scopes = [s for s in token.scopes_csv.split(",") if s] if token.scopes_csv else []
    return KeyIntrospection(
        token_id=token.id,
        name=token.name,
        project_id=token.project_id,
        env=env,
        scopes=scopes,
    )
