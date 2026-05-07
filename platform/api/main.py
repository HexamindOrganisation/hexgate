from contextlib import asynccontextmanager

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from db import engine, init_db
from relay import registry
from schemas import (
    AgentRead,
    AgentUpdate,
    RegisterAgentRequest,
    RegisterAgentResponse,
    TokenListItem,
    TokenMintRequest,
    TokenMintResponse,
)
from services import (
    delete_dev_token,
    ensure_default_project,
    find_token_by_secret,
    get_agent,
    list_agents,
    list_dev_tokens,
    mask_secret,
    mint_dev_token,
    register_manifest,
    update_agent,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with Session(engine) as session:
        ensure_default_project(session)
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

    POC behaviour: the header is optional so the dashboard (which has no user
    session concept yet) can keep calling these endpoints. When the header is
    present we validate it, reject on mismatch, and touch last_used_at so the
    UI shows real activity. Tighten to required in Phase C.
    """
    if authorization is None:
        return
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="malformed authorization header")
    secret = authorization.removeprefix("Bearer ").strip()
    if find_token_by_secret(session, secret) is None:
        raise HTTPException(status_code=401, detail="invalid fortify key")


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


@app.get("/health")
def health() -> dict[str, str]:
    """Unversioned liveness probe."""
    return {"status": "ok", "service": "fortify-api"}


v1 = APIRouter(prefix="/v1")


@v1.get("/health")
def v1_health() -> dict[str, str]:
    return {"status": "ok", "service": "fortify-api", "version": "v1"}


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


@v1.get("/projects/{project_id}/agents", response_model=list[AgentRead])
def api_list_agents(
    project_id: str, session: Session = Depends(get_session)
) -> list[AgentRead]:
    ensure_default_project(session)
    agents = list_agents(session, project_id)
    return [
        AgentRead(
            id=a.id,
            name=a.name,
            agent_yaml=a.agent_yaml,
            policy_yaml=a.policy_yaml,
            system_md=a.system_md,
            updated_at=a.updated_at,
        )
        for a in agents
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
    return AgentRead(
        id=agent.id,
        name=agent.name,
        agent_yaml=agent.agent_yaml,
        policy_yaml=agent.policy_yaml,
        system_md=agent.system_md,
        updated_at=agent.updated_at,
    )


@v1.put("/projects/{project_id}/agents/{name}", response_model=AgentRead)
def api_update_agent(
    project_id: str,
    name: str,
    body: AgentUpdate,
    session: Session = Depends(get_session),
) -> AgentRead:
    agent = update_agent(
        session,
        project_id,
        name,
        agent_yaml=body.agent_yaml,
        policy_yaml=body.policy_yaml,
        system_md=body.system_md,
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return AgentRead(
        id=agent.id,
        name=agent.name,
        agent_yaml=agent.agent_yaml,
        policy_yaml=agent.policy_yaml,
        system_md=agent.system_md,
        updated_at=agent.updated_at,
    )


@v1.post("/agents", response_model=RegisterAgentResponse, status_code=201)
def api_register_agent(
    body: RegisterAgentRequest,
    project_id: str = Depends(require_project),
    session: Session = Depends(get_session),
) -> RegisterAgentResponse:
    """SDK-facing: register/upsert an agent manifest under the bearer's project."""
    version, created = register_manifest(session, project_id, body.manifest)
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


app.include_router(v1)
