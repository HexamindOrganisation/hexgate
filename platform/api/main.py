from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from db import engine, init_db
from schemas import TokenListItem, TokenMintRequest, TokenMintResponse
from services import (
    delete_dev_token,
    ensure_default_project,
    list_dev_tokens,
    mask_secret,
    mint_dev_token,
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


@app.get("/health")
def health() -> dict[str, str]:
    """Unversioned liveness probe."""
    return {"status": "ok", "service": "fortify-api"}


v1 = APIRouter(prefix="/v1")


@v1.get("/health")
def v1_health() -> dict[str, str]:
    return {"status": "ok", "service": "fortify-api", "version": "v1"}


@v1.get("/projects/{project_id}/tokens", response_model=list[TokenListItem])
def list_tokens(project_id: str, session: Session = Depends(get_session)) -> list[TokenListItem]:
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


@v1.post("/projects/{project_id}/tokens", response_model=TokenMintResponse, status_code=201)
def mint_token(
    project_id: str,
    body: TokenMintRequest,
    session: Session = Depends(get_session),
) -> TokenMintResponse:
    ensure_default_project(session)  # POC: lazy-create so single project works out of the box
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


app.include_router(v1)
