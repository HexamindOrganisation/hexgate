"""Powerless-token LLM gateway for the live demo.

The ONE always-on service that holds the real ``OPENAI_API_KEY``. It must run
*outside* the per-visitor container — if the real key lived in the container,
the visitor's code could read it. Each demo session gets a **session token**
that is safe to print because it's powerless:

  * expires after a short TTL,
  * capped to N requests (a crude but robust abuse guard — swap in LiteLLM if
    you want true $-accounting),
  * pinned to an allowlist of models,
  * only works against this gateway.

Two surfaces:
  * ``POST /admin/session``  (auth: master key) → mint a session token. boot.py
    calls this once per container and injects the result as the agent's
    ``OPENAI_API_KEY`` + ``OPENAI_BASE_URL``.
  * ``POST /v1/chat/completions`` / ``GET /v1/models`` (auth: session token) →
    OpenAI-compatible, validated, forwarded (streaming passthrough).

Token store is in-memory: fine for a single gateway with short-lived tokens.
For multi-replica, back it with Redis.

Env:
  OPENAI_API_KEY            real upstream key (required)
  GATEWAY_MASTER_KEY        admin secret for /admin/session (required)
  GATEWAY_UPSTREAM_BASE     upstream base (default https://api.openai.com/v1)
  GATEWAY_ALLOWED_MODELS    csv allowlist (default gpt-4o-mini)
  GATEWAY_DEFAULT_TTL       seconds (default 600)
  GATEWAY_DEFAULT_MAX_REQ   requests per session (default 40)
  GATEWAY_DAILY_REQ_CAP     global kill-switch across all sessions (default 50000)

Run:  uvicorn deploy.gateway.app:app --port 9000
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

UPSTREAM = os.environ.get("GATEWAY_UPSTREAM_BASE", "https://api.openai.com/v1").rstrip("/")
ALLOWED_MODELS = {
    m.strip()
    for m in os.environ.get("GATEWAY_ALLOWED_MODELS", "gpt-4o-mini").split(",")
    if m.strip()
}
DEFAULT_TTL = int(os.environ.get("GATEWAY_DEFAULT_TTL", "600"))
DEFAULT_MAX_REQ = int(os.environ.get("GATEWAY_DEFAULT_MAX_REQ", "40"))
DAILY_REQ_CAP = int(os.environ.get("GATEWAY_DAILY_REQ_CAP", "50000"))


@dataclass
class Session:
    expires_at: float
    max_requests: int
    models: set[str]
    used: int = 0


@dataclass
class _State:
    sessions: dict[str, Session] = field(default_factory=dict)
    global_used: int = 0  # rough daily kill-switch (process-lifetime)


_state = _State()
app = FastAPI(title="Hexgate demo LLM gateway")


def _now() -> float:
    return time.time()


def _real_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise HTTPException(500, "gateway misconfigured: OPENAI_API_KEY unset")
    return key


# --------------------------------------------------------------------------
# Admin: mint a powerless session token
# --------------------------------------------------------------------------
@app.post("/admin/session")
async def mint_session(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    master = os.environ.get("GATEWAY_MASTER_KEY")
    if not master or authorization != f"Bearer {master}":
        raise HTTPException(401, "bad master key")

    body = await request.json() if await request.body() else {}
    ttl = int(body.get("ttl", DEFAULT_TTL))
    max_req = int(body.get("max_requests", DEFAULT_MAX_REQ))
    models = set(body.get("models") or ALLOWED_MODELS) & ALLOWED_MODELS or set(ALLOWED_MODELS)

    token = "sk-demo-" + secrets.token_urlsafe(24)
    _state.sessions[token] = Session(
        expires_at=_now() + ttl, max_requests=max_req, models=models
    )
    return JSONResponse(
        {"token": token, "ttl": ttl, "max_requests": max_req, "models": sorted(models)}
    )


def _authorize(authorization: str | None, model: str | None) -> Session:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing session token")
    token = authorization.split(" ", 1)[1]
    sess = _state.sessions.get(token)
    if sess is None:
        raise HTTPException(401, "unknown or revoked session token")
    if _now() > sess.expires_at:
        _state.sessions.pop(token, None)
        raise HTTPException(401, "session token expired")
    if sess.used >= sess.max_requests:
        raise HTTPException(429, "session request cap reached")
    if _state.global_used >= DAILY_REQ_CAP:
        raise HTTPException(503, "demo gateway daily cap reached")
    if model is not None and model not in sess.models:
        raise HTTPException(403, f"model {model!r} not allowed for this session")
    return sess


# --------------------------------------------------------------------------
# OpenAI-compatible surface
# --------------------------------------------------------------------------
@app.get("/v1/models")
async def list_models(authorization: str | None = Header(default=None)) -> JSONResponse:
    sess = _authorize(authorization, None)
    return JSONResponse(
        {"object": "list", "data": [{"id": m, "object": "model"} for m in sorted(sess.models)]}
    )


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request, authorization: str | None = Header(default=None)
):
    body = await request.json()
    sess = _authorize(authorization, body.get("model"))
    # Consume one unit up front so a hung/streaming call still counts.
    sess.used += 1
    _state.global_used += 1

    stream = bool(body.get("stream"))
    headers = {"Authorization": f"Bearer {_real_key()}", "Content-Type": "application/json"}
    url = f"{UPSTREAM}/chat/completions"

    if not stream:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=body, headers=headers)
        return JSONResponse(r.json(), status_code=r.status_code)

    async def _proxy_stream():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", url, json=body, headers=headers) as r:
                async for chunk in r.aiter_raw():
                    yield chunk

    return StreamingResponse(_proxy_stream(), media_type="text/event-stream")
