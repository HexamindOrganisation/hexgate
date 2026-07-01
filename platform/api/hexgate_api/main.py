import asyncio
import base64
from datetime import datetime
import hashlib
import logging
import os
from contextlib import asynccontextmanager

from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError
from dotenv import load_dotenv
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
from hexgate_api.models import (
    Agent,
    AgentVersion,
    Invitation,
    Organization,
    OrganizationMember,
    Project,
    User,
)
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.audit import (
    WINDOW_HOURS,
    AuditEventOutOfWindow,
    AuditPayloadTooLarge,
    anomalies,
    prepare_date_range,
    insert_decision,
    list_decisions,
    summarize,
    timeseries,
    validate_event_window,
)
from hexgate_api.core.clickhouse import ping as clickhouse_ping
from hexgate_api.core.db import async_session_factory, get_session, init_db
from hexgate_api.core.keystore import FileKeyStore
from hexgate_api.core.relay import registry
from hexgate_api.deps.clickhouse import _audit_unavailable, require_clickhouse
from hexgate_api.deps.tokens import require_project
from hexgate_api.deps.ws import ws_require_org_member, ws_require_project
from hexgate_api.schemas import (
    AgentManifest,
    AgentManifestView,
    AgentRead,
    AgentUpdate,
    AuditAnomaly,
    AuditDecisionPage,
    AuditOutcome,
    AuditSummary,
    AuditTimeseriesPoint,
    AuditWindow,
    DecisionAccepted,
    DecisionEvent,
    InvitationCreate,
    InvitationPreview,
    InvitationRead,
    KeyIntrospection,
    MemberRead,
    MemberUpdate,
    OrgCreate,
    OrgRead,
    OrgUpdate,
    OrgWithRole,
    PolicyValidationError,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    RegisterAgentRequest,
    RegisterAgentResponse,
    TokenListItem,
    TokenMintRequest,
    TokenMintResponse,
    ValidatePolicyRequest,
    ValidatePolicyResponse,
)
from hexgate_api.services import (
    backfill_bundles,
    delete_dev_token,
    ensure_default_project,
    find_token_by_secret,
    get_agent,
    get_latest_agent_version_id,
    get_latest_agent_versions_map,
    list_agents,
    list_dev_tokens,
    mask_secret,
    mint_dev_token,
    register_manifest,
    update_agent,
)


# Load .env into os.environ before any HEXGATE_* read (CORS + keystore
# resolve at import time). Real env vars still take precedence.
load_dotenv()

keystore = FileKeyStore()
_log = logging.getLogger(__name__)


def _demo_enabled() -> bool:
    """Whether single-tenant demo mode is on (see platform/api/demo.py).

    Off by default. When on, the API exposes a *passwordless* ``/v1/demo-login``
    for the seeded admin — safe only in an ephemeral throwaway container. (The
    same-origin dashboard serving is no longer demo-specific; see
    :func:`spa.mount_spa`, wired in both modes.)
    """
    return os.environ.get("HEXGATE_DEMO", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _configure_email_sender() -> None:
    """Swap the dev stderr sender for Resend if both env vars are set.

    Three cases, three log levels:
      * both set → INFO "Resend wired" — production happy path.
      * neither set → INFO "dev stderr sender" — clean dev mode.
      * exactly one set → WARNING naming the missing var — operator
        misconfig; falls back to stderr rather than half-broken Resend.
    """
    from hexgate_api.core.mailer import (
        ResendEmailSender,
        StderrEmailSender,
        set_email_sender,
    )

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_addr = os.environ.get("HEXGATE_EMAIL_FROM", "").strip()
    if api_key and from_addr:
        set_email_sender(ResendEmailSender(api_key=api_key, from_addr=from_addr))
        _log.info("email: Resend sender wired (from=%s)", from_addr)
        return
    # Reset to stderr explicitly so a re-config (test, lifespan-restart)
    # that clears env vars doesn't leave a stale Resend sender wired.
    set_email_sender(StderrEmailSender())
    if api_key or from_addr:
        missing = "HEXGATE_EMAIL_FROM" if api_key else "RESEND_API_KEY"
        present = "RESEND_API_KEY" if api_key else "HEXGATE_EMAIL_FROM"
        _log.warning(
            "email: partial Resend config — %s is set but %s is not. "
            "Falling back to dev stderr sender; real mail will NOT be sent.",
            present,
            missing,
        )
    else:
        _log.info(
            "email: dev stderr sender — set RESEND_API_KEY and HEXGATE_EMAIL_FROM "
            "to deliver real mail (verification + password reset)."
        )


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
    # SPA catch-all goes on LAST — after the OAuth router just mounted — so
    # /{path} never shadows /v1/auth/google/*. (Static /v1 routes are already
    # registered at import; only the OAuth router mounts here at startup, so the
    # SPA must follow it.) Demo mode mounts the same SPA + a passwordless login.
    if _demo_enabled():
        from hexgate_api.demo import enable_demo

        enable_demo(app)
    else:
        from hexgate_api.core.spa import mount_spa

        mount_spa(app)
    async with async_session_factory() as session:
        await ensure_default_project(session)
        # Backfill signed bundles for seeded agents so they're served via
        # WASM on the first request, not just after their first edit.
        await backfill_bundles(session, keystore.sign)
    # Don't fail startup on unreachable ClickHouse — /ready surfaces it.
    if not clickhouse_ping():
        _log.warning(
            "ClickHouse unreachable at startup; audit endpoints will 503 until reachable"
        )
    # Surface deployment config at startup so a misconfig shows in logs
    # rather than as a silent browser CORS/cookie failure.
    from hexgate_api.auth import _cookie_secure, _dashboard_url

    _log.info(
        "hexgate-api startup config: cors_origins=%s cookie_secure=%s dashboard_url=%s",
        _cors_origins(),
        _cookie_secure(),
        _dashboard_url(),
    )
    _configure_email_sender()
    if _demo_enabled():
        _log.warning(
            "⚠ HEXGATE_DEMO is ON — /v1/demo-login grants a PASSWORDLESS session "
            "for the seeded admin. Use ONLY in an ephemeral throwaway container, "
            "NEVER on a persistent/real deployment."
        )
    yield


app = FastAPI(title="Hexgate API", version="0.1.0", lifespan=lifespan)


_DEFAULT_CORS_ORIGINS = ["http://localhost:5173"]


def _cors_origins() -> list[str]:
    """Allowed browser origins from comma-separated ``HEXGATE_CORS_ORIGINS``.

    Entries are trailing-slash/whitespace-stripped to match the ``Origin``
    header. Unset or unparseable falls back to the dev default. No wildcard:
    credentialed CORS forbids it, so production must list explicit origins.
    """
    raw = os.environ.get("HEXGATE_CORS_ORIGINS", "").strip()
    if not raw:
        return _DEFAULT_CORS_ORIGINS
    parsed = [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]
    return parsed or _DEFAULT_CORS_ORIGINS


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Cookie/dashboard-user auth gates (see hexgate_api/deps/). Imported here, not
# in the top import block, so they land after the keystore is defined: these
# pull in auth.py, whose _session_secret() lazy-imports the keystore singleton
# from this module. The two auth surfaces (dashboard humans vs SDK machines)
# stay separate by design — see m3-platform-auth.md, "the dual-auth-surface
# insight".
from hexgate_api.deps.identity import require_user  # noqa: E402
from hexgate_api.deps.org import (  # noqa: E402
    require_org_admin,
    require_org_admin_or_self,
    require_org_member,
    require_org_membership,
)
from hexgate_api.deps.project import require_project_admin  # noqa: E402


def _readiness() -> tuple[dict[str, str], int]:
    """Build the readiness body and its HTTP status. Returns 503 when ClickHouse
    is unreachable so probes deroute the pod instead of sending it ingest traffic
    that would only 503 — k8s keys off the status code, not the body."""
    reachable = clickhouse_ping()
    body = {
        "status": "ok" if reachable else "unavailable",
        "service": "hexgate-api",
        "clickhouse": "ok" if reachable else "unreachable",
    }
    return body, 200 if reachable else 503


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness — must not touch downstream deps, or an outage cascades into
    restarts. Dependency checks live in /ready."""
    return {"status": "ok", "service": "hexgate-api"}


@app.get("/ready")
def ready(response: Response) -> dict[str, str]:
    """Readiness — pings ClickHouse; 503 when unreachable."""
    body, response.status_code = _readiness()
    return body


v1 = APIRouter(prefix="/v1")


@v1.get("/health")
async def v1_health() -> dict[str, str]:
    return {"status": "ok", "service": "hexgate-api", "version": "v1"}


@v1.get("/ready")
def v1_ready(response: Response) -> dict[str, str]:
    body, response.status_code = _readiness()
    return {**body, "version": "v1"}


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
    dependencies=[Depends(require_org_member)],
)
async def api_get_agent(
    project_id: str,
    name: str,
    response: Response,
    session: AsyncSession = Depends(get_session),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> AgentRead | Response:
    """Return an agent's YAMLs + signed bundle (dashboard read path).

    Cookie-authed via :func:`require_org_member`. The SDK's bearer-only
    equivalent is :func:`api_get_agent_by_token` at ``GET
    /v1/agents/{name}`` — same response shape, project derived from
    the token instead of the URL.

    Supports ETag-based conditional GETs: the response carries the
    bundle's ``wasm_hash`` as an ``ETag`` header. A subsequent request
    with ``If-None-Match: <wasm_hash>`` returns ``304 Not Modified``
    when the bundle hasn't changed.
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

    from hexgate.security import AgentPolicy
    from hexgate.security.constraints import (
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
    """SDK-facing: register/upsert an agent manifest under the bearer's project.

    Threads ``keystore.sign`` through so first-time registers get a real
    signed WASM bundle (and a starter role-aware policy) — re-registers
    don't touch the agent's policy_yaml, so the operator's dashboard
    edits are preserved.
    """
    version, created = await register_manifest(
        session, project_id, body.manifest, sign=keystore.sign
    )
    response.status_code = 201 if created else 200
    return RegisterAgentResponse(
        agent_id=version.agent_id,
        agent_version_id=version.id,
        name=body.manifest.name,
        version=version.version,
        content_hash=version.content_hash,
        created=created,
    )


@v1.post(
    "/audit/decisions",
    response_model=DecisionAccepted,
    status_code=202,
    tags=["audit"],
)
async def ingest_decision(
    body: DecisionEvent,
    project_id: str = Depends(require_project),
    session: AsyncSession = Depends(get_session),
    clickhouse_client=Depends(require_clickhouse),
) -> DecisionAccepted:
    """Ingest one policy decision. project_id (bearer), received_at (CH default),
    and agent_version_id (platform lookup) are server-resolved.

    Idempotency: the SDK SHOULD retry a failed or ambiguous send (503,
    timeout) with the SAME event_id. The ingest path is idempotent because
    the storage engine (ReplacingMergeTree, event_id in the sort key)
    collapses duplicates on background merges — eventual, so counts may
    briefly include a retry until the next merge. Do NOT mint a fresh
    event_id per attempt; that turns a retry into a real duplicate.
    """
    try:
        validate_event_window(body.occurred_at)
    except AuditEventOutOfWindow as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    agent_version_id = await get_latest_agent_version_id(
        session, project_id, body.agent_name
    )

    try:
        # Sync client + wait_for_async_insert=1 → a real network round-trip;
        # run it off the event loop like the read handlers below.
        await asyncio.to_thread(
            insert_decision,
            clickhouse_client,
            event=body,
            project_id=project_id,
            agent_version_id=agent_version_id,
        )
    except AuditPayloadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except OperationalError as exc:  # transient transport failure — retryable
        _log.warning("audit insert failed (transient): %s", exc)
        raise _audit_unavailable()
    except ClickHouseError as exc:  # storage rejected the row — retry won't help
        _log.error("audit insert rejected by ClickHouse: %s", exc)
        raise HTTPException(status_code=422, detail="audit event rejected by storage")

    return DecisionAccepted(event_id=body.event_id)


# Dashboard audit reads — project-scoped aggregation, cookie-authed like the
# other dashboard reads (org membership via the project path param).
#
# ``role`` filter semantics: absent = no filter; ``role=`` (empty value) =
# the no-role bucket. No sentinel string is reserved on the wire — the
# dashboard renders "(none)" purely as a display label.


@v1.get(
    "/projects/{project_id}/audit/summary",
    response_model=AuditSummary,
    dependencies=[Depends(require_org_member)],
    tags=["audit"],
)
async def api_audit_summary(
    project_id: str,
    window: AuditWindow = "24h",
    agent: str | None = None,
    role: str | None = None,
    tool: str | None = None,
    user: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    clickhouse_client=Depends(require_clickhouse),
) -> AuditSummary:
    start_date, end_date = prepare_date_range(start_date, end_date)
    try:
        # The clickhouse_connect client is sync — run it off the event loop
        # so a slow aggregation can't stall every other in-flight request.
        data = await asyncio.to_thread(
            summarize,
            clickhouse_client,
            project_id=project_id,
            since_hours=WINDOW_HOURS[window],
            agent=agent,
            role=role,
            tool=tool,
            user=user,
            start_date=start_date,
            end_date=end_date,
        )
    except ClickHouseError:
        raise _audit_unavailable()
    return AuditSummary.model_validate(data)


@v1.get(
    "/projects/{project_id}/audit/timeseries",
    response_model=list[AuditTimeseriesPoint],
    dependencies=[Depends(require_org_member)],
    tags=["audit"],
)
async def api_audit_timeseries(
    project_id: str,
    window: AuditWindow = "24h",
    agent: str | None = None,
    role: str | None = None,
    tool: str | None = None,
    user: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    clickhouse_client=Depends(require_clickhouse),
) -> list[AuditTimeseriesPoint]:
    start_date, end_date = prepare_date_range(start_date, end_date)
    try:
        return await asyncio.to_thread(
            timeseries,
            clickhouse_client,
            project_id=project_id,
            since_hours=WINDOW_HOURS[window],
            agent=agent,
            role=role,
            tool=tool,
            user=user,
            start_date=start_date,
            end_date=end_date,
        )
    except ClickHouseError:
        raise _audit_unavailable()


@v1.get(
    "/projects/{project_id}/audit/decisions",
    response_model=AuditDecisionPage,
    dependencies=[Depends(require_org_member)],
    tags=["audit"],
)
async def api_audit_decisions(
    project_id: str,
    window: AuditWindow = "24h",
    agent: str | None = None,
    role: str | None = None,
    tool: str | None = None,
    user: str | None = None,
    outcome: AuditOutcome | None = None,
    session_id: str | None = None,
    limit: int = 25,
    offset: int = 0,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    clickhouse_client=Depends(require_clickhouse),
) -> AuditDecisionPage:
    start_date, end_date = prepare_date_range(start_date, end_date)
    try:
        page = await asyncio.to_thread(
            list_decisions,
            clickhouse_client,
            project_id=project_id,
            since_hours=WINDOW_HOURS[window],
            agent=agent,
            role=role,
            tool=tool,
            user=user,
            outcome=outcome,
            session_id=session_id,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
            start_date=start_date,
            end_date=end_date,
        )
    except ClickHouseError:
        raise _audit_unavailable()
    return page


@v1.get(
    "/projects/{project_id}/audit/anomalies",
    response_model=list[AuditAnomaly],
    dependencies=[Depends(require_org_member)],
    tags=["audit"],
)
async def api_audit_anomalies(
    project_id: str,
    window: AuditWindow = "24h",
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    clickhouse_client=Depends(require_clickhouse),
) -> list[AuditAnomaly]:
    start_date, end_date = prepare_date_range(start_date, end_date)
    try:
        return await asyncio.to_thread(
            anomalies,
            clickhouse_client,
            project_id=project_id,
            since_hours=WINDOW_HOURS[window],
            start_date=start_date,
            end_date=end_date,
        )
    except ClickHouseError:
        raise _audit_unavailable()


@v1.get("/agents/{name}", response_model=AgentRead)
async def api_get_agent_by_token(
    name: str,
    response: Response,
    project_id: str = Depends(require_project),
    session: AsyncSession = Depends(get_session),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> AgentRead | Response:
    """SDK-facing read of an agent — project comes from the bearer token.

    The cookie-authed dashboard counterpart at
    ``GET /v1/projects/{id}/agents/{name}`` returns the same shape;
    this route is the CLI's policy-refresh entry point and is
    bearer-only via :func:`require_project`.

    ETag semantics mirror the cookie route — the SDK's per-run
    conditional GET (``If-None-Match: <wasm_hash>`` → ``304``) costs
    one short round-trip when the bundle hasn't changed.
    """
    await ensure_default_project(session)
    agent = await get_agent(session, project_id, name)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")

    bundle_hash = (
        hashlib.sha256(agent.compiled_wasm).hexdigest()
        if agent.compiled_wasm is not None
        else None
    )
    etag = f'"{bundle_hash}"' if bundle_hash else None

    if etag and if_none_match and if_none_match.strip() == etag:
        return Response(status_code=304, headers={"ETag": etag})

    if etag:
        response.headers["ETag"] = etag
    return _agent_read(agent)


@v1.get("/me/key", response_model=KeyIntrospection)
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


@v1.websocket("/serve")
async def ws_serve(
    websocket: WebSocket,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Producer socket for ``hexgate serve`` — project derived from token.

    The CLI connects with two subprotocols offered: ``bearer.<key>`` and
    ``hexgate.v1``. ``ws_require_project`` validates the bearer and
    resolves it to the token's project (no project_id in the URL — the
    biscuit *is* the project context). On a successful handshake the
    server echoes ``hexgate.v1`` back; the bearer subprotocol is
    consumed and never mirrored.
    """
    project_id = await ws_require_project(websocket, session)
    if project_id is None:
        return  # handshake already closed with 4401
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
async def ws_chat(
    websocket: WebSocket,
    project_id: str,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Consumer socket for dashboard Playground sessions.

    Cookie-authed: the dashboard's JS WebSocket reaches for the
    ``hexgate_session`` cookie automatically. ``ws_require_org_member``
    verifies it + checks the caller is a member of the project's org
    before ``accept()`` runs. Anonymous / cross-org connects close
    with 4401 before the handshake completes.
    """
    user = await ws_require_org_member(websocket, project_id, session)
    if user is None:
        return  # close already sent
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

from hexgate_api.auth import (  # noqa: E402 — placed late so keystore is initialised
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


# ---------------------------------------------------------------------------
# M3 Phase 4 — Organization CRUD
#
# Read-your-own / create-new / read-by-id / update-name. Member
# management + invitations land in subsequent steps but share these
# dependencies and schemas.
# ---------------------------------------------------------------------------


def _org_read(org: Organization) -> OrgRead:
    return OrgRead(id=org.id, slug=org.slug, name=org.name, created_at=org.created_at)


@v1.get("/orgs", tags=["orgs"])
async def api_list_orgs(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[OrgWithRole]:
    """List every org the active user belongs to, with their role on each.

    Used by the dashboard's org switcher (Phase 5) — one request, no
    N+1 over memberships, role on the edge so the UI knows what
    actions to enable per row.

    **Repair path:** if the user has zero orgs (e.g., the
    ``on_after_register`` hook errored after FastAPI-Users committed
    the User row, or the user predates the personal-default-org
    bootstrap), call :func:`ensure_personal_default_org` here. The
    dashboard's first call on each session goes through this endpoint,
    so the repair is opportunistic and silent. The helper is
    idempotent on the "user already owns an org" invariant, so a
    concurrent repair-then-create race can't double-bootstrap.
    """
    from hexgate_api.services import ensure_personal_default_org, list_orgs_for_user

    rows = await list_orgs_for_user(session, user.id)
    if not rows:
        await ensure_personal_default_org(session, user)
        await session.commit()
        rows = await list_orgs_for_user(session, user.id)
    return [
        OrgWithRole(
            id=o.id, slug=o.slug, name=o.name, created_at=o.created_at, role=role
        )
        for o, role in rows
    ]


@v1.post("/orgs", status_code=201, tags=["orgs"])
async def api_create_org(
    body: OrgCreate,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> OrgRead:
    """Create a new Organization. Caller becomes the owner in the same
    transaction (no transient state with zero members).

    ``body.slug`` is optional — derived from the name when omitted, with
    the same collision-fallback chain :func:`ensure_personal_default_org`
    uses for signup. When the caller-supplied slug collides, we return
    409 rather than silently picking a different one — explicit failure
    so the UI can prompt for a tweak.
    """
    from hexgate_api.services import (
        _email_to_slug_base,
        _generate_unique_org_slug,
        create_org,
    )

    if body.slug:
        existing = (
            await session.exec(
                select(Organization).where(Organization.slug == body.slug)
            )
        ).first()
        if existing is not None:
            raise HTTPException(
                status_code=409, detail=f"slug {body.slug!r} is already taken"
            )
        slug = body.slug
    else:
        # Derive from name using the same sanitizer as the email-prefix
        # path; if the derived slug is contested, the helper picks a
        # numbered or hex-suffixed variant.
        slug = await _generate_unique_org_slug(session, _email_to_slug_base(body.name))

    org = await create_org(session, name=body.name, slug=slug, owner_user_id=user.id)
    return _org_read(org)


@v1.get("/orgs/{org_id}", tags=["orgs"])
async def api_get_org(
    membership: tuple[User, OrganizationMember] = Depends(require_org_membership),
    session: AsyncSession = Depends(get_session),
) -> OrgRead:
    """Detail view of one org. Membership required (any role)."""
    _, member = membership
    org = await session.get(Organization, member.org_id)
    # ``require_org_membership`` already 404'd if org is missing; the
    # `is not None` is paranoia for the type checker.
    assert org is not None
    return _org_read(org)


@v1.patch("/orgs/{org_id}", tags=["orgs"])
async def api_update_org(
    body: OrgUpdate,
    membership: tuple[User, OrganizationMember] = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> OrgRead:
    """Update name and/or slug. ``admin`` or ``owner`` role required.

    Slug changes break existing /orgs/{old-slug}/... bookmarks; we let
    callers do it because the row's ``id`` is the stable handle every
    FK points at (the slug is a URL helper, mutable on purpose).
    Returns 409 if the new slug collides with another org's.
    """
    _, member = membership
    org = await session.get(Organization, member.org_id)
    assert org is not None

    if body.slug is not None and body.slug != org.slug:
        existing = (
            await session.exec(
                select(Organization).where(Organization.slug == body.slug)
            )
        ).first()
        if existing is not None:
            raise HTTPException(
                status_code=409, detail=f"slug {body.slug!r} is already taken"
            )
        org.slug = body.slug

    if body.name is not None:
        org.name = body.name

    session.add(org)
    await session.commit()
    await session.refresh(org)
    return _org_read(org)


# ---------------------------------------------------------------------------
# M3 Phase 4 step 3 — Organization member management
#
# Service-layer helpers (list_org_members / change_member_role /
# remove_member / LastOwnerError) already exist; these handlers just
# wrap them with HTTP semantics.
# ---------------------------------------------------------------------------


def _member_read(member: OrganizationMember, user: User) -> MemberRead:
    """Shape the (membership, user) join into the wire row."""
    return MemberRead(
        user_id=user.id,
        email=user.email,
        role=member.role,
        joined_at=member.created_at,
    )


@v1.get("/orgs/{org_id}/members", tags=["orgs"])
async def api_list_members(
    membership: tuple[User, OrganizationMember] = Depends(require_org_membership),
    session: AsyncSession = Depends(get_session),
) -> list[MemberRead]:
    """List all members of an org. Any member can read.

    The role gating intentionally stops at "any member" rather than
    "admin/owner" — every member has a legitimate need to know who
    else is in the org (e.g., to know who to ask for promotion).
    """
    from hexgate_api.services import list_org_members

    _, member = membership
    rows = await list_org_members(session, member.org_id)
    return [_member_read(m, u) for m, u in rows]


@v1.patch("/orgs/{org_id}/members/{user_id}", tags=["orgs"])
async def api_update_member_role(
    user_id: str,
    body: MemberUpdate,
    membership: tuple[User, OrganizationMember] = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> MemberRead:
    """Promote / demote a member. Admin or owner role required.

    Two service-layer refusals surface here as HTTP errors:

      * ``RoleEscalationError`` → 403 — the caller (admin or owner)
        tried to assign a role above their own rank. Admins can't
        mint owners by going through PATCH any more than they can
        through the invitation path; the rank check is centralised
        on :func:`_can_invite_role`.
      * ``LastOwnerError`` → 409 — demoting the only owner would
        orphan the org. Catches self-demotion too via the owner count.

    Returns the updated row so the dashboard can re-render the badge
    without a follow-up GET.
    """
    from hexgate_api.services import (
        LastOwnerError,
        RoleEscalationError,
        change_member_role,
    )

    _, caller_member = membership
    try:
        updated = await change_member_role(
            session,
            org_id=caller_member.org_id,
            user_id=user_id,
            new_role=body.role,
            caller_role=caller_member.role,
        )
    except RoleEscalationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LastOwnerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="member not found")

    # Look up the user so we can return MemberRead's email field.
    user = await session.get(User, user_id)
    assert user is not None  # FK guarantee
    return _member_read(updated, user)


@v1.delete("/orgs/{org_id}/members/{user_id}", status_code=204, tags=["orgs"])
async def api_remove_member(
    user_id: str,
    membership: tuple[User, OrganizationMember] = Depends(require_org_admin_or_self),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Remove a member. Admin/owner can remove anyone; plain members
    can only remove themselves (the "leave organization" flow).

    Refuses with 409 when the removal would leave the org with zero
    owners — promote another member to owner first, then leave.

    Returns 204 No Content on success (REST norm for DELETE).
    """
    from hexgate_api.services import LastOwnerError, remove_member

    _, caller_member = membership
    try:
        removed = await remove_member(
            session, org_id=caller_member.org_id, user_id=user_id
        )
    except LastOwnerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="member not found")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# M3 Phase 4 step 4 — Invitation routes
#
# Admin/owner mints an invitation; mailer ships a magic-link email
# with /invites/{id}/accept. Preview is public-readable so the invitee
# sees who invited them before sign-in; accept is cookie-authed and
# strictly email-matched.
# ---------------------------------------------------------------------------


def _invitation_read(invitation: Invitation, inviter: User) -> InvitationRead:
    """Shape the (Invitation, inviter User) join into the dashboard
    list row. Includes the invitation id so the dashboard's Cancel
    button has a row to address; the strict email-match guard on
    ``POST /invites/{id}/accept`` keeps id exposure from being an
    impersonation vector — see InvitationRead's docstring."""
    return InvitationRead(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        invited_by_email=inviter.email,
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
    )


@v1.post("/orgs/{org_id}/invites", status_code=201, tags=["orgs"])
async def api_create_invitation(
    body: InvitationCreate,
    membership: tuple[User, OrganizationMember] = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> InvitationRead:
    """Mint a pending invitation + email it. Admin or owner required.

    Role escalation guard fires in :func:`services.create_invitation` —
    admins can only invite at-or-below their level (no minting owner
    invites then accepting them yourself to promote). Surfaced as
    400 with a specific detail message.

    The route also looks up the org name + inviter email for the email
    body (one extra query each; cheap).
    """
    from hexgate_api.services import (
        InvitationError,
        create_invitation,
        send_invitation_email,
    )

    caller, caller_member = membership
    try:
        invitation = await create_invitation(
            session,
            org_id=caller_member.org_id,
            email=body.email,
            role=body.role,
            invited_by=caller_member,
        )
    except InvitationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Email the invitee. Failures here are logged but don't fail the
    # API call — the invite row exists; a re-send can happen via a
    # follow-up POST to the same email (which cancels this one and
    # mints a fresh link).
    org = await session.get(Organization, caller_member.org_id)
    assert org is not None
    try:
        await send_invitation_email(
            invitation=invitation, org_name=org.name, inviter_email=caller.email
        )
    except Exception:
        # Mailer failure — log via the auth logger so it shows up in
        # the same stderr block operators are already watching. Invitee
        # address is PII-redacted (same rule as mailer.py); the org slug
        # stays so support can grep by tenant.
        from hexgate_api.auth import logger as auth_logger
        from hexgate_api.core.mailer import _redact_email

        auth_logger.exception(
            "failed to send invitation email to %s for org %s",
            _redact_email(invitation.email),
            org.slug,
        )

    return _invitation_read(invitation, caller)


@v1.get("/orgs/{org_id}/invites", tags=["orgs"])
async def api_list_invitations(
    membership: tuple[User, OrganizationMember] = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> list[InvitationRead]:
    """List pending invitations for the org. Admin or owner required.

    Only non-terminal invites (no ``accepted_at``, no ``revoked_at``)
    show up. Already-accepted invites surface implicitly as new
    OrganizationMember rows via ``GET /members``.
    """
    from hexgate_api.services import list_pending_invitations

    _, caller_member = membership
    rows = await list_pending_invitations(session, caller_member.org_id)
    return [_invitation_read(inv, inv_user) for inv, inv_user in rows]


@v1.get("/invites/{invitation_id}", tags=["invitations"])
async def api_get_invitation_preview(
    invitation_id: str,
    session: AsyncSession = Depends(get_session),
) -> InvitationPreview:
    """Public-readable preview of an invitation.

    Returns 404 for unknown ids; 410 Gone for terminal invites
    (already accepted/revoked/expired). Lets the invitee land on the
    accept page and see what they're being invited to BEFORE
    authenticating. The invite id is UUID v4 (unguessable enough);
    the accept POST is what requires auth + strict email match.
    """
    from hexgate_api.services import _is_invitation_terminal, find_invitation

    invitation = await find_invitation(session, invitation_id)
    if invitation is None:
        raise HTTPException(status_code=404, detail="invitation not found")
    if _is_invitation_terminal(invitation):
        raise HTTPException(
            status_code=410, detail="invitation expired or already used"
        )

    org = await session.get(Organization, invitation.org_id)
    inviter = await session.get(User, invitation.invited_by_user_id)
    assert org is not None and inviter is not None  # FK guarantee
    return InvitationPreview(
        email=invitation.email,
        role=invitation.role,
        invited_by_email=inviter.email,
        org_id=org.id,
        org_name=org.name,
        org_slug=org.slug,
        expires_at=invitation.expires_at,
    )


@v1.post("/invites/{invitation_id}/accept", tags=["invitations"])
async def api_accept_invitation(
    invitation_id: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> MemberRead:
    """Consume an invitation. Cookie-authenticated; email must match.

    Returns the newly-created (or already-existing) ``OrganizationMember``
    row so the dashboard can immediately drop the user into the org's
    view without a follow-up ``/orgs`` round-trip.

    HTTP codes mirror the service-layer exception hierarchy:
      * 404 — unknown invitation id
      * 410 — expired
      * 409 — already accepted or revoked
      * 403 — email mismatch ("this invite isn't for you")
    """
    from hexgate_api.services import (
        InvitationAlreadyConsumed,
        InvitationEmailMismatch,
        InvitationExpired,
        accept_invitation,
        find_invitation,
    )

    invitation = await find_invitation(session, invitation_id)
    if invitation is None:
        raise HTTPException(status_code=404, detail="invitation not found")

    try:
        member = await accept_invitation(
            session, invitation=invitation, accepting_user=user
        )
    except InvitationExpired as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except InvitationAlreadyConsumed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvitationEmailMismatch as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return _member_read(member, user)


@v1.delete("/invites/{invitation_id}", status_code=204, tags=["invitations"])
async def api_revoke_invitation(
    invitation_id: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Cancel a pending invitation. Two callers are authorised:

      * admin / owner of the inviting org — the "cancel" use case
      * the invited user themselves (by email match) — the "decline" use
        case, so the invitee can close out an unwanted invite without
        joining

    404 for unknown; 409 for already-terminal invites. Idempotent on
    success — calling DELETE on an already-revoked invite is a no-op
    that still returns 204.
    """
    from hexgate_api.services import (
        ROLE_ADMIN,
        ROLE_OWNER,
        find_invitation,
        find_member,
        revoke_invitation,
    )

    invitation = await find_invitation(session, invitation_id)
    if invitation is None:
        raise HTTPException(status_code=404, detail="invitation not found")

    if invitation.email.lower() == user.email.lower():
        # Invitee declining their own invite.
        await revoke_invitation(session, invitation)
        return Response(status_code=204)

    # Otherwise check whether the caller is an admin or owner of the org.
    caller_member = await find_member(
        session, org_id=invitation.org_id, user_id=user.id
    )
    if caller_member is None or caller_member.role not in {
        ROLE_OWNER,
        ROLE_ADMIN,
    }:
        raise HTTPException(
            status_code=403,
            detail="only admins/owners or the invitee can cancel an invitation",
        )

    await revoke_invitation(session, invitation)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# M3 Phase 4 step 5 — Project CRUD
#
# Create + list live under /orgs/{org_id}/projects (need to know the
# org); read + rename live under /projects/{project_id} (the project
# row knows its own org). DELETE intentionally not shipped here —
# cascade across Agent / DevToken / AgentVersion / Tool needs its
# own focused commit.
# ---------------------------------------------------------------------------


def _project_read(project: Project) -> ProjectRead:
    return ProjectRead(
        id=project.id,
        org_id=project.org_id,
        name=project.name,
        created_at=project.created_at,
    )


@v1.post("/orgs/{org_id}/projects", status_code=201, tags=["orgs"])
async def api_create_project(
    body: ProjectCreate,
    membership: tuple[User, OrganizationMember] = Depends(require_org_membership),
    session: AsyncSession = Depends(get_session),
) -> ProjectRead:
    """Create a project under an org. Any member can create — projects
    are a workspace primitive, not a destructive op. The intent is to
    tighten to admin-only later if needed (one-line change in the dep).

    409 if a project with the same name already exists in this org
    (the user probably meant to switch to the existing one).
    """
    from hexgate_api.services import ProjectNameTakenError, create_project

    _, caller_member = membership
    try:
        project = await create_project(
            session, org_id=caller_member.org_id, name=body.name
        )
    except ProjectNameTakenError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _project_read(project)


@v1.get("/orgs/{org_id}/projects", tags=["orgs"])
async def api_list_projects(
    membership: tuple[User, OrganizationMember] = Depends(require_org_membership),
    session: AsyncSession = Depends(get_session),
) -> list[ProjectRead]:
    """List every project inside an org. Any member can list — the
    dashboard's project picker consumes this."""
    from hexgate_api.services import list_projects

    _, caller_member = membership
    rows = await list_projects(session, caller_member.org_id)
    return [_project_read(p) for p in rows]


@v1.get("/projects/{project_id}", tags=["projects"])
async def api_get_project(
    project_id: str,
    _user: User = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> ProjectRead:
    """Detail view of a single project. The ``require_org_member`` dep
    resolves the project's org_id and gates on the caller being a
    member — same shape as the existing project-scoped routes
    (/agents, /tokens) so the auth surface stays uniform."""
    project = await session.get(Project, project_id)
    assert project is not None  # require_org_member already 404'd
    return _project_read(project)


@v1.patch("/projects/{project_id}", tags=["projects"])
async def api_update_project(
    project_id: str,
    body: ProjectUpdate,
    _membership: tuple[User, OrganizationMember] = Depends(require_project_admin),
    session: AsyncSession = Depends(get_session),
) -> ProjectRead:
    """Rename a project. Admin or owner required. 409 on name collision
    with another project in the same org."""
    from hexgate_api.services import ProjectNameTakenError, update_project_name

    try:
        project = await update_project_name(
            session, project_id=project_id, name=body.name
        )
    except ProjectNameTakenError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # require_project_admin already 404'd if the project was missing;
    # update_project_name returning None at this point would be a race.
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return _project_read(project)


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
            "[hexgate] Google OAuth enabled (HEXGATE_GOOGLE_CLIENT_ID set)",
            file=sys.stderr,
        )
    else:
        print(
            "[hexgate] Google OAuth disabled — set HEXGATE_GOOGLE_CLIENT_ID "
            "+ HEXGATE_GOOGLE_CLIENT_SECRET to enable",
            file=sys.stderr,
        )


app.include_router(v1)

# The SPA catch-all is mounted in the lifespan (after the OAuth router), NOT
# here — registering it at import would shadow the lifespan-mounted
# /v1/auth/google/* routes, since Starlette matches in registration order.
