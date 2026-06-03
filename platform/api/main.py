import base64
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError
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
from models import Agent, AgentVersion
from sqlmodel import Session

from audit import (
    NO_VALUE_LABEL,
    WINDOW_HOURS,
    AuditPayloadTooLarge,
    bucket_minutes_for,
    insert_decision,
    list_decisions,
    summarize,
    timeseries,
)
from biscuits import (
    TokenError,
    TokenSignatureError,
    parse_envelope,
    verify_token,
)
from clickhouse import get_clickhouse, ping as clickhouse_ping
from db import engine, init_db
from keystore import FileKeyStore
from relay import registry
from schemas import (
    AgentManifest,
    AgentManifestView,
    AgentRead,
    AgentUpdate,
    AuditDecisionPage,
    AuditSummary,
    AuditTimeseriesPoint,
    AuditWindow,
    DecisionAccepted,
    DecisionEvent,
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
    get_latest_agent_version_id,
    get_latest_agent_versions_map,
    list_agents,
    list_dev_tokens,
    mask_secret,
    mint_dev_token,
    register_manifest,
    update_agent,
)


keystore = FileKeyStore()
_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    keystore.ensure_keypair()
    with Session(engine) as session:
        ensure_default_project(session)
        # Backfill signed bundles for seeded agents so they're served via
        # WASM on the first request, not just after their first edit.
        backfill_bundles(session, keystore.sign)
    # Don't fail startup on unreachable ClickHouse — /ready surfaces it.
    if not clickhouse_ping():
        _log.warning(
            "ClickHouse unreachable at startup; /v1/audit/decisions will 503 until reachable"
        )
    yield


app = FastAPI(title="Fortify API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_session():
    with Session(engine) as session:
        yield session


def optional_dev_token(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> None:
    """Validate Authorization: Bearer <fortify_key> when present.

    Two gates run when a header is supplied:

    1. **Signature verification** — parse the envelope, decode the Biscuit,
       check it chains to the platform's root public key. Rejects tampered
       tokens and tokens minted by some other platform instance.
    2. **Revocation lookup** — confirm the exact secret is still in the
       ``DevToken`` table and update ``last_used_at``. Catches revocation
       even if the Biscuit signature is intrinsically valid.

    POC behaviour: the header itself remains optional so the dashboard
    (no user-session concept yet) can keep calling these endpoints
    unauthenticated. Tighten to required once the dashboard auth lands.
    """
    if authorization is None:
        return
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
    if find_token_by_secret(session, secret) is None:
        raise HTTPException(status_code=401, detail="unknown or revoked fortify key")


def require_project(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
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
    token = find_token_by_secret(session, secret)
    if token is None:
        raise HTTPException(status_code=401, detail="invalid fortify key")
    return token.project_id


def _readiness() -> tuple[dict[str, str], int]:
    """Build the readiness body and its HTTP status. Returns 503 when ClickHouse
    is unreachable so probes deroute the pod instead of sending it ingest traffic
    that would only 503 — k8s keys off the status code, not the body."""
    reachable = clickhouse_ping()
    body = {
        "status":     "ok" if reachable else "unavailable",
        "service":    "fortify-api",
        "clickhouse": "ok" if reachable else "unreachable",
    }
    return body, 200 if reachable else 503


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness — must not touch downstream deps, or an outage cascades into
    restarts. Dependency checks live in /ready."""
    return {"status": "ok", "service": "fortify-api"}


@app.get("/ready")
def ready(response: Response) -> dict[str, str]:
    """Readiness — pings ClickHouse; 503 when unreachable."""
    body, response.status_code = _readiness()
    return body


v1 = APIRouter(prefix="/v1")


@v1.get("/health")
def v1_health() -> dict[str, str]:
    return {"status": "ok", "service": "fortify-api", "version": "v1"}


@v1.get("/ready")
def v1_ready(response: Response) -> dict[str, str]:
    body, response.status_code = _readiness()
    return {**body, "version": "v1"}


@v1.get("/.well-known/keys")
def well_known_keys() -> dict[str, object]:
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


@v1.get("/projects/{project_id}/tokens", response_model=list[TokenListItem])
def list_tokens(
    project_id: str, session: Session = Depends(get_session)
) -> list[TokenListItem]:
    tokens = list_dev_tokens(session, project_id)
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
    "/projects/{project_id}/tokens", response_model=TokenMintResponse, status_code=201
)
def mint_token(
    project_id: str,
    body: TokenMintRequest,
    session: Session = Depends(get_session),
) -> TokenMintResponse:
    ensure_default_project(
        session
    )  # POC: lazy-create so single project works out of the box
    token, full = mint_dev_token(
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


@v1.delete("/projects/{project_id}/tokens/{token_id}", status_code=204)
def revoke_token(
    project_id: str,
    token_id: str,
    session: Session = Depends(get_session),
) -> None:
    ok = delete_dev_token(session, project_id, token_id)
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


@v1.get("/projects/{project_id}/agents", response_model=list[AgentRead])
def api_list_agents(
    project_id: str, session: Session = Depends(get_session)
) -> list[AgentRead]:
    ensure_default_project(session)
    return [_agent_read(a) for a in list_agents(session, project_id)]


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
)
def api_list_agent_manifests(
    project_id: str, session: Session = Depends(get_session)
) -> list[AgentManifestView]:
    """Bulk read of every agent's latest registered manifest.

    One row per Agent. Agents that exist but have no version registered
    come back with ``manifest=None``.
    """
    ensure_default_project(session)
    agents = list_agents(session, project_id)
    latest_by_agent = get_latest_agent_versions_map(session, [a.id for a in agents])
    return [
        _build_agent_manifest_view(agent, latest_by_agent.get(agent.id))
        for agent in agents
    ]


@v1.get("/projects/{project_id}/agents/{name}", response_model=AgentRead)
def api_get_agent(
    project_id: str,
    name: str,
    session: Session = Depends(get_session),
    _auth: None = Depends(optional_dev_token),
) -> AgentRead:
    ensure_default_project(session)
    agent = get_agent(session, project_id, name)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return _agent_read(agent)


@v1.put("/projects/{project_id}/agents/{name}", response_model=AgentRead)
def api_update_agent(
    project_id: str,
    name: str,
    body: AgentUpdate,
    session: Session = Depends(get_session),
) -> AgentRead:
    ensure_default_project(session)
    agent = update_agent(
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
)
def api_validate_policy(
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
def api_register_agent(
    body: RegisterAgentRequest,
    response: Response,
    project_id: str = Depends(require_project),
    session: Session = Depends(get_session),
) -> RegisterAgentResponse:
    """SDK-facing: register/upsert an agent manifest under the bearer's project."""
    version, created = register_manifest(session, project_id, body.manifest)
    response.status_code = 201 if created else 200
    return RegisterAgentResponse(
        agent_id=version.agent_id,
        agent_version_id=version.id,
        name=body.manifest.name,
        version=version.version,
        content_hash=version.content_hash,
        created=created,
    )


# Time-window for accepting decision events.
CLOCK_SKEW_FUTURE = timedelta(minutes=5)
RETENTION_WINDOW = timedelta(days=90)


def _audit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="audit log temporarily unavailable",
        headers={"Retry-After": "5"},
    )


def require_clickhouse():
    """Resolve the ClickHouse client as a dependency, mapping connect failures
    to 503 — get_clickhouse() connects eagerly, so without this the raise
    escapes dependency resolution as an uncaught 500."""
    try:
        return get_clickhouse()
    except ClickHouseError as exc:
        _log.warning("ClickHouse unreachable resolving audit client: %s", exc)
        raise _audit_unavailable()


@v1.post(
    "/audit/decisions",
    response_model=DecisionAccepted,
    status_code=202,
)
def ingest_decision(
    body: DecisionEvent,
    project_id: str = Depends(require_project),
    session: Session = Depends(get_session),
    clickhouse_client=Depends(require_clickhouse),
) -> DecisionAccepted:
    """Ingest one policy decision. project_id (bearer), received_at (CH default),
    and agent_version_id (platform lookup) are server-resolved."""
    now = datetime.now(timezone.utc)
    if body.occurred_at > now + CLOCK_SKEW_FUTURE:
        raise HTTPException(status_code=400, detail="occurred_at is in the future")
    if body.occurred_at < now - RETENTION_WINDOW:
        raise HTTPException(
            status_code=400, detail="occurred_at is older than retention window"
        )

    agent_version_id = get_latest_agent_version_id(session, project_id, body.agent_name)

    try:
        insert_decision(
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
        raise HTTPException(
            status_code=422, detail="audit event rejected by storage"
        )

    return DecisionAccepted(event_id=body.event_id)


# Dashboard audit read path — project-scoped aggregation over policy_decision.
# Unauthenticated POC posture, matching the other dashboard reads (/agents,
# /tokens); the eventual gate is the read_audit scope (see below). All three
# window off WINDOW_HOURS (bounded by the 90-day TTL); FastAPI 422s a bad window
# via the AuditWindow Literal.
#
# TODO(auth): gate behind the read_audit scope once dashboard auth lands —
# same future-auth hook the other /projects/{project_id} reads carry.
# NO_VALUE_LABEL ("(none)") is the breakdown label for an empty role; drill-down
# on it must filter real no-role rows, so translate it back to "".
def _role_filter(role: Optional[str]) -> Optional[str]:
    return "" if role == NO_VALUE_LABEL else role


@v1.get("/projects/{project_id}/audit/summary", response_model=AuditSummary)
def api_audit_summary(
    project_id: str,
    window: AuditWindow = "24h",
    agent: Optional[str] = None,
    role: Optional[str] = None,
    tool: Optional[str] = None,
    q: Optional[str] = None,
    clickhouse_client=Depends(require_clickhouse),
) -> AuditSummary:
    try:
        data = summarize(
            clickhouse_client,
            project_id=project_id,
            since_hours=WINDOW_HOURS[window],
            agent=agent,
            role=_role_filter(role),
            tool=tool,
            q=q,
        )
    except ClickHouseError:
        raise _audit_unavailable()
    return AuditSummary.model_validate(data)


@v1.get(
    "/projects/{project_id}/audit/timeseries",
    response_model=list[AuditTimeseriesPoint],
)
def api_audit_timeseries(
    project_id: str,
    window: AuditWindow = "24h",
    agent: Optional[str] = None,
    role: Optional[str] = None,
    tool: Optional[str] = None,
    q: Optional[str] = None,
    clickhouse_client=Depends(require_clickhouse),
) -> list[AuditTimeseriesPoint]:
    try:
        return timeseries(
            clickhouse_client,
            project_id=project_id,
            since_hours=WINDOW_HOURS[window],
            bucket_minutes=bucket_minutes_for(window),
            agent=agent,
            role=_role_filter(role),
            tool=tool,
            q=q,
        )
    except ClickHouseError:
        raise _audit_unavailable()


@v1.get(
    "/projects/{project_id}/audit/decisions",
    response_model=AuditDecisionPage,
)
def api_audit_decisions(
    project_id: str,
    window: AuditWindow = "24h",
    agent: Optional[str] = None,
    role: Optional[str] = None,
    tool: Optional[str] = None,
    outcome: Optional[Literal["allow", "deny", "needs_approval"]] = None,
    session_id: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 25,
    offset: int = 0,
    clickhouse_client=Depends(require_clickhouse),
) -> AuditDecisionPage:
    # NO_VALUE_LABEL is the breakdown label for an empty role; translate it back
    # to "" so drill-down on the "(none)" bucket filters real no-role rows.
    role_filter = "" if role == NO_VALUE_LABEL else role
    try:
        page = list_decisions(
            clickhouse_client,
            project_id=project_id,
            since_hours=WINDOW_HOURS[window],
            agent=agent,
            role=role_filter,
            tool=tool,
            outcome=outcome,
            session_id=session_id,
            q=q,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )
    except ClickHouseError:
        raise _audit_unavailable()
    return page


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


app.include_router(v1)
