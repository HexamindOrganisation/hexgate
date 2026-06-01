import base64
import hashlib
from contextlib import asynccontextmanager

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from models import Agent, AgentVersion, OrganizationMember, Project, User
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from biscuits import (
    TokenError,
    TokenSignatureError,
    parse_envelope,
    verify_token,
)
from db import async_session_factory, get_session, init_db
from keystore import FileKeyStore
from relay import registry
from schemas import (
    AgentManifest,
    AgentManifestView,
    AgentRead,
    AgentUpdate,
    PolicyValidationError,
    RegisterAgentRequest,
    RegisterAgentResponse,
    TokenListItem,
    TokenMintRequest,
    TokenMintResponse,
    ValidatePolicyRequest,
    ValidatePolicyResponse,
)
from services import (
    backfill_bundles,
    delete_dev_token,
    ensure_default_project,
    find_token_by_secret,
    get_agent,
    get_latest_agent_versions_map,
    list_agents,
    list_dev_tokens,
    mask_secret,
    mint_dev_token,
    register_manifest,
    update_agent,
)


keystore = FileKeyStore()


@asynccontextmanager
async def lifespan(app_: FastAPI):
    await init_db()
    keystore.ensure_keypair()
    # OAuth router mounting waits on the keystore — its state-token
    # secret is derived from the keystore's private key (see
    # auth._oauth_state_secret). Doing this at module load would race
    # the lifespan; the include here runs once at startup, before any
    # request reaches the app.
    _maybe_mount_oauth_routers()
    async with async_session_factory() as session:
        await ensure_default_project(session)
        # Backfill signed bundles for seeded agents so they're served via
        # WASM on the first request, not just after their first edit.
        await backfill_bundles(session, keystore.sign)
    yield


app = FastAPI(title="Fortify API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _validate_sdk_token(
    authorization: str, session: AsyncSession
) -> None:
    """Validate an ``Authorization: Bearer <fortify_key>`` biscuit envelope.

    Shared between ``optional_dev_token`` (allows missing header) and
    ``require_user_or_sdk_token`` (uses the biscuit as one of two
    permissibility paths). Raises 401 on signature or revocation failure;
    returns None on success.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="malformed authorization header")
    secret = authorization.removeprefix("Bearer ").strip()

    # Signature gate
    try:
        _, _, biscuit_b64 = parse_envelope(secret)
    except TokenError:
        raise HTTPException(status_code=401, detail="malformed fortify key") from None
    try:
        verify_token(biscuit_b64, keystore.public_key_bytes())
    except TokenSignatureError:
        raise HTTPException(
            status_code=401, detail="invalid fortify key signature"
        ) from None

    # Revocation gate
    if await find_token_by_secret(session, secret) is None:
        raise HTTPException(status_code=401, detail="unknown or revoked fortify key")


async def optional_dev_token(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Validate Authorization: Bearer <fortify_key> when present.

    Two gates run when a header is supplied:

    1. **Signature verification** — parse the envelope, decode the Biscuit,
       check it chains to the platform's root public key.
    2. **Revocation lookup** — confirm the exact secret is still in the
       ``DevToken`` table and update ``last_used_at``.

    POC behaviour: the header itself remains optional so the dashboard
    (no user-session concept yet) can keep calling these endpoints
    unauthenticated. Routes that DO require some auth use
    ``require_org_member`` (humans) or ``require_user_or_sdk_token``
    (either).
    """
    if authorization is None:
        return
    await _validate_sdk_token(authorization, session)


async def require_project(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> str:
    """Resolve `Authorization: Bearer <fortify_key>` to a project_id.

    Used by SDK-facing endpoints (e.g. POST /v1/agents) where the caller
    has only an API key, not a project id in the URL.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="missing or malformed authorization header"
        )
    secret = authorization.removeprefix("Bearer ").strip()
    token = await find_token_by_secret(session, secret)
    if token is None:
        raise HTTPException(status_code=401, detail="invalid fortify key")
    return token.project_id


# ---------------------------------------------------------------------------
# M3 Phase 2 — dashboard-user dependencies (auth-as-dev-header scaffolding)
#
# These are the human-facing equivalents of ``require_project``: they gate
# routes by which org the active user belongs to. The "active user" today
# comes from an ``X-Dev-User: <user_id>`` request header — a placeholder
# that Phase 3 replaces with a real session cookie from FastAPI Users
# without touching any caller of these dependencies.
#
# The two auth surfaces (dashboard humans vs SDK machines) stay separate
# by design — see m3-platform-auth.md, "The dual-auth-surface insight".
# ---------------------------------------------------------------------------


from auth import current_active_user_optional  # noqa: E402 — placed after  # type: ignore[import]
# the keystore is defined so auth.py's _session_secret() lazy-import works.


async def require_user(
    cookie_user: User | None = Depends(current_active_user_optional),
    x_dev_user: str | None = Header(default=None, alias="X-Dev-User"),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the active dashboard user, cookie-first, header-fallback.

    The cookie session (FastAPI Users) is the production path from
    Phase 3a onward; ``X-Dev-User`` stays accepted during the
    transition so the existing dashboard keeps working unchanged
    until Phase 5 builds the real sign-in UI. The header path comes
    out once the dashboard switches.
    """
    if cookie_user is not None:
        return cookie_user

    if x_dev_user:
        user = await session.get(User, x_dev_user)
        if user is not None and user.is_active:
            return user

    raise HTTPException(
        status_code=401, detail="missing or invalid authentication"
    )


async def require_org_member(
    project_id: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Gate a project-scoped route on the active user's org membership.

    Resolves the project's ``org_id``, then confirms the active user has
    an ``OrganizationMember`` row for that org. Returns the ``User`` so
    handlers can use it directly without a second lookup.

    Status codes: ``404`` if the project doesn't exist (don't leak that
    fact by 403'ing); ``403`` if the project exists but the user isn't
    a member of its org.
    """
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    membership = (await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.user_id == user.id,
            OrganizationMember.org_id == project.org_id,
        )
    )).first()
    if membership is None:
        raise HTTPException(status_code=403, detail="not a member of this org")
    return user


async def require_user_or_sdk_token(
    project_id: str,
    x_dev_user: str | None = Header(default=None, alias="X-Dev-User"),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Accept EITHER an SDK biscuit OR a dashboard user with org membership.

    Used on routes both humans (via dashboard) and machines (via SDK)
    legitimately hit — today that's ``GET /v1/projects/{p}/agents/{name}``,
    which the SDK polls every turn for policy refresh and the dashboard
    reads to render the agent editor.

    Either path succeeds independently; the request is only rejected if
    neither is present-and-valid.
    """
    # SDK path: Authorization header carries a biscuit. Validate it via
    # the same signature + revocation gates the existing dependency uses.
    if authorization:
        await _validate_sdk_token(authorization, session)
        return

    # Dashboard path: X-Dev-User + org membership.
    if not x_dev_user:
        raise HTTPException(
            status_code=401,
            detail="missing authentication (X-Dev-User or Bearer token)",
        )
    user = await session.get(User, x_dev_user)
    if user is None:
        raise HTTPException(status_code=401, detail="unknown user")
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    membership = (await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.user_id == user.id,
            OrganizationMember.org_id == project.org_id,
        )
    )).first()
    if membership is None:
        raise HTTPException(status_code=403, detail="not a member of this org")


@app.get("/health")
async def health() -> dict[str, str]:
    """Unversioned liveness probe."""
    return {"status": "ok", "service": "fortify-api"}


v1 = APIRouter(prefix="/v1")


@v1.get("/health")
async def v1_health() -> dict[str, str]:
    return {"status": "ok", "service": "fortify-api", "version": "v1"}


@v1.get("/.well-known/keys")
async def well_known_keys() -> dict[str, object]:
    """Publish the platform's signing public key + fingerprint.

    JWKS-shaped so we can grow into multi-key publishing later without
    breaking clients. Lets dashboards and CLIs sanity-check that what
    their SDK has embedded matches what this platform is signing with.
    """
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


@v1.get(
    "/projects/{project_id}/tokens",
    response_model=list[TokenListItem],
    dependencies=[Depends(require_org_member)],
)
async def list_tokens(
    project_id: str, session: AsyncSession = Depends(get_session)
) -> list[TokenListItem]:
    tokens = await list_dev_tokens(session, project_id)
    return [
        TokenListItem(
            id=t.id,
            name=t.name,
            masked=mask_secret(t.secret),
            scopes=t.scopes_csv.split(",") if t.scopes_csv else [],
            created_at=t.created_at,
            last_used_at=t.last_used_at,
        )
        for t in tokens
    ]


@v1.post(
    "/projects/{project_id}/tokens",
    response_model=TokenMintResponse,
    status_code=201,
    dependencies=[Depends(require_org_member)],
)
async def mint_token(
    project_id: str,
    body: TokenMintRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenMintResponse:
    await ensure_default_project(
        session
    )  # POC: lazy-create so single project works out of the box
    token, full = await mint_dev_token(
        session,
        project_id=project_id,
        name=body.name,
        scopes=body.scopes,
        env=body.env,
        signing_key_bytes=keystore._private_key_bytes(),
    )
    return TokenMintResponse(
        id=token.id,
        name=token.name,
        full=full,
        masked=mask_secret(full),
        scopes=token.scopes_csv.split(",") if token.scopes_csv else [],
        created_at=token.created_at,
    )


@v1.delete(
    "/projects/{project_id}/tokens/{token_id}",
    status_code=204,
    dependencies=[Depends(require_org_member)],
)
async def revoke_token(
    project_id: str,
    token_id: str,
    session: AsyncSession = Depends(get_session),
) -> None:
    ok = await delete_dev_token(session, project_id, token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="token not found")


def _agent_read(agent: Agent) -> AgentRead:
    """Shared serialiser used by GET, list, and PUT — keeps the wire format aligned."""
    return AgentRead(
        id=agent.id,
        name=agent.name,
        agent_yaml=agent.agent_yaml,
        policy_yaml=agent.policy_yaml,
        system_md=agent.system_md,
        updated_at=agent.updated_at,
        bundle_wasm_b64=(
            base64.b64encode(agent.compiled_wasm).decode("ascii")
            if agent.compiled_wasm is not None
            else None
        ),
        bundle_manifest=agent.bundle_manifest,
        bundle_signature_b64=(
            base64.b64encode(agent.bundle_signature).decode("ascii")
            if agent.bundle_signature is not None
            else None
        ),
    )


@v1.get(
    "/projects/{project_id}/agents",
    response_model=list[AgentRead],
    dependencies=[Depends(require_org_member)],
)
async def api_list_agents(
    project_id: str, session: AsyncSession = Depends(get_session)
) -> list[AgentRead]:
    await ensure_default_project(session)
    return [_agent_read(a) for a in await list_agents(session, project_id)]


def _build_agent_manifest_view(
    agent: Agent, agent_version: AgentVersion | None
) -> AgentManifestView:
    """Build the dashboard manifest envelope from an Agent + its latest version.

    Rehydrates ``AgentVersion.manifest`` (a JSON snapshot, validated against
    :class:`AgentManifest` at registration time) back into the typed shape,
    or returns the envelope with ``manifest=None`` when no usable version
    exists — either no version row at all, or a row whose ``manifest`` column
    is NULL (nullable on the model; only reachable via direct DB writes).
    """
    if agent_version is None or agent_version.manifest is None:
        return AgentManifestView(name=agent.name, updated_at=agent.updated_at)
    return AgentManifestView(
        name=agent.name,
        manifest=AgentManifest.model_validate(agent_version.manifest),
        version=agent_version.version,
        content_hash=agent_version.content_hash,
        updated_at=agent_version.created_at,
    )


# Declared before the ``/agents/{name}`` route so FastAPI matches the literal
# ``manifest`` segment instead of binding it as a name path parameter.
@v1.get(
    "/projects/{project_id}/agents/manifest",
    response_model=list[AgentManifestView],
    dependencies=[Depends(require_org_member)],
)
async def api_list_agent_manifests(
    project_id: str, session: AsyncSession = Depends(get_session)
) -> list[AgentManifestView]:
    """Bulk read of every agent's latest registered manifest.

    One row per Agent. Agents that exist but have no version registered
    come back with ``manifest=None``.
    """
    await ensure_default_project(session)
    agents = await list_agents(session, project_id)
    latest_by_agent = await get_latest_agent_versions_map(
        session, [a.id for a in agents]
    )
    return [
        _build_agent_manifest_view(agent, latest_by_agent.get(agent.id))
        for agent in agents
    ]


@v1.get(
    "/projects/{project_id}/agents/{name}",
    response_model=AgentRead,
    dependencies=[Depends(require_user_or_sdk_token)],
)
async def api_get_agent(
    project_id: str,
    name: str,
    response: Response,
    session: AsyncSession = Depends(get_session),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> AgentRead | Response:
    """Return an agent's YAMLs + signed bundle.

    Supports ETag-based conditional GETs: the response carries the
    bundle's ``wasm_hash`` as an ``ETag`` header. A subsequent request
    with ``If-None-Match: <wasm_hash>`` returns ``304 Not Modified``
    when the bundle hasn't changed — so the SDK's per-run refresh costs
    one short round-trip instead of base64-decoding the wasm again.
    """
    await ensure_default_project(session)
    agent = await get_agent(session, project_id, name)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")

    # ETag is a quoted opaque string per RFC 7232. We use the wasm_hash
    # (a sha256 hex digest); falls back to None when no bundle is stored.
    bundle_hash = (
        hashlib.sha256(agent.compiled_wasm).hexdigest()
        if agent.compiled_wasm is not None
        else None
    )
    etag = f'"{bundle_hash}"' if bundle_hash else None

    if etag and if_none_match and if_none_match.strip() == etag:
        # 304 — no body, just the ETag so the client can re-confirm.
        return Response(status_code=304, headers={"ETag": etag})

    if etag:
        response.headers["ETag"] = etag
    return _agent_read(agent)


@v1.put(
    "/projects/{project_id}/agents/{name}",
    response_model=AgentRead,
    dependencies=[Depends(require_org_member)],
)
async def api_update_agent(
    project_id: str,
    name: str,
    body: AgentUpdate,
    session: AsyncSession = Depends(get_session),
) -> AgentRead:
    await ensure_default_project(session)
    agent = await update_agent(
        session,
        project_id,
        name,
        agent_yaml=body.agent_yaml,
        policy_yaml=body.policy_yaml,
        system_md=body.system_md,
        # Compile + sign the policy into a WASM bundle at save time, using
        # the platform's root key (same key that signs biscuits).
        sign=keystore.sign,
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return _agent_read(agent)


@v1.post(
    "/projects/{project_id}/agents/{name}/validate",
    response_model=ValidatePolicyResponse,
    dependencies=[Depends(require_org_member)],
)
async def api_validate_policy(
    project_id: str,  # noqa: ARG001 — routed by FastAPI, scope-checked by future auth
    name: str,  # noqa: ARG001 — same
    body: ValidatePolicyRequest,
) -> ValidatePolicyResponse:
    """Parse ``policy.yaml`` end-to-end + check every ``constraints`` string.

    Server-side validation keeps one source of truth on grammar — the same
    parsers the SDK enforces with at run time. Handles both shapes:

    * flat single-policy document → validated as one :class:`AgentPolicy`
    * inline-roles document (top-level ``roles:`` map) → each entry
      validated as an :class:`AgentPolicy`, then every constraint inside
      every tool is parsed against the M1 grammar.

    Returns a flat list of ``{role, line, message}`` diagnostics. ``role``
    is ``None`` for top-level YAML / schema errors; populated when the
    failure lives inside a specific role's section.
    """
    import yaml
    from yaml.error import MarkedYAMLError
    from pydantic import ValidationError

    from fortify.security import AgentPolicy
    from fortify.security.constraints import (
        ConstraintParseError,
        parse_constraint,
    )

    errors: list[PolicyValidationError] = []
    try:
        parsed = yaml.safe_load(body.policy_yaml) or {}
    except MarkedYAMLError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark else None
        return ValidatePolicyResponse(
            ok=False,
            errors=[
                PolicyValidationError(
                    line=line,
                    message=f"YAML parse: {exc.problem or exc}",
                )
            ],
        )

    def _check_policy(policy: AgentPolicy, role_name: str | None) -> None:
        for tool_name, tool_policy in policy.tools.items():
            for constraint in tool_policy.constraints:
                try:
                    parse_constraint(constraint)
                except ConstraintParseError as exc:
                    errors.append(
                        PolicyValidationError(
                            role=role_name,
                            message=f"{tool_name}: {exc}",
                        )
                    )

    if isinstance(parsed.get("roles"), dict):
        # Inline-roles shape: validate each role's policy.
        for role_name, role_spec in parsed["roles"].items():
            try:
                role_policy = AgentPolicy.model_validate(role_spec or {})
            except ValidationError as exc:
                errors.append(
                    PolicyValidationError(
                        role=role_name,
                        message=f"policy schema: {exc.errors()[0]['msg']}",
                    )
                )
                continue
            _check_policy(role_policy, role_name)
    else:
        # Flat single-policy shape.
        try:
            policy = AgentPolicy.model_validate(parsed)
        except ValidationError as exc:
            return ValidatePolicyResponse(
                ok=False,
                errors=[
                    PolicyValidationError(
                        message=f"policy schema: {exc.errors()[0]['msg']}",
                    )
                ],
            )
        _check_policy(policy, None)

    return ValidatePolicyResponse(ok=not errors, errors=errors)


@v1.post("/agents", response_model=RegisterAgentResponse)
async def api_register_agent(
    body: RegisterAgentRequest,
    response: Response,
    project_id: str = Depends(require_project),
    session: AsyncSession = Depends(get_session),
) -> RegisterAgentResponse:
    """SDK-facing: register/upsert an agent manifest under the bearer's project."""
    version, created = await register_manifest(session, project_id, body.manifest)
    response.status_code = 201 if created else 200
    return RegisterAgentResponse(
        agent_id=version.agent_id,
        agent_version_id=version.id,
        name=body.manifest.name,
        version=version.version,
        content_hash=version.content_hash,
        created=created,
    )


@v1.websocket("/projects/{project_id}/serve")
async def ws_serve(websocket: WebSocket, project_id: str) -> None:
    """Producer socket for an agent serve process."""
    await websocket.accept()
    await registry.attach_serve(project_id, websocket)
    try:
        while True:
            payload = await websocket.receive_json()
            if isinstance(payload, dict) and payload.get("type") == "hello":
                agent_name = payload.get("agent")
                await registry.set_agent_name(
                    project_id, agent_name if isinstance(agent_name, str) else None
                )
                continue
            await registry.relay_to_chat(project_id, payload)
    except WebSocketDisconnect:
        pass
    finally:
        await registry.detach_serve(project_id, websocket)


@v1.websocket("/projects/{project_id}/chat")
async def ws_chat(websocket: WebSocket, project_id: str) -> None:
    """Consumer socket for dashboard Playground sessions."""
    await websocket.accept()
    await registry.attach_chat(project_id, websocket)
    try:
        while True:
            payload = await websocket.receive_json()
            await registry.relay_to_serve(project_id, payload)
    except WebSocketDisconnect:
        pass
    finally:
        await registry.detach_chat(project_id, websocket)


# ---------------------------------------------------------------------------
# M3 Phase 3a — FastAPI Users routers
#
# Mounted under /v1/auth/* and /v1/users/* so they ride the same versioned
# prefix as the rest of the API. The library provides one router per
# concern; we include the cookie auth + register routers now and the
# verify / reset-password / oauth routers in 3b / 3c.
# ---------------------------------------------------------------------------

from auth import (  # noqa: E402 — placed late so keystore is initialised
    UserCreate,
    UserRead,
    UserUpdate,
    auth_backend,
    build_google_oauth_router,
    fastapi_users,
)

v1.include_router(
    fastapi_users.get_auth_router(auth_backend),
    prefix="/auth/cookie",
    tags=["auth"],
)
v1.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    prefix="/auth",
    tags=["auth"],
)
# Phase 3b — email verification (POST /auth/request-verify-token + /auth/verify)
# and password reset (POST /auth/forgot-password + /auth/reset-password). Both
# routers use the UserManager email hooks (on_after_request_verify +
# on_after_forgot_password) to send the magic-link tokens through the mailer.
v1.include_router(
    fastapi_users.get_verify_router(UserRead),
    prefix="/auth",
    tags=["auth"],
)
v1.include_router(
    fastapi_users.get_reset_password_router(),
    prefix="/auth",
    tags=["auth"],
)
v1.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate),
    prefix="/users",
    tags=["users"],
)

def _maybe_mount_oauth_routers() -> None:
    """Mount the Phase 3c OAuth router(s) iff env-configured.

    Called from the lifespan once the keystore is initialised — its
    private key derives the OAuth state-token secret. With no Google
    credentials in env, this is a no-op and ``make platform-api``
    works out of the box; flipping the two env vars and restarting
    the server turns Google sign-in on. The router goes onto ``app``
    directly (not ``v1``) so we don't double-include the rest of v1
    that ``app.include_router(v1)`` below already mounted.
    """
    import sys

    google_router = build_google_oauth_router()
    if google_router is not None:
        app.include_router(
            google_router,
            prefix="/v1/auth/google",
            tags=["auth"],
        )
        print(
            "[fortify] Google OAuth enabled (FORTIFY_GOOGLE_CLIENT_ID set)",
            file=sys.stderr,
        )
    else:
        print(
            "[fortify] Google OAuth disabled — set FORTIFY_GOOGLE_CLIENT_ID "
            "+ FORTIFY_GOOGLE_CLIENT_SECRET to enable",
            file=sys.stderr,
        )


app.include_router(v1)
