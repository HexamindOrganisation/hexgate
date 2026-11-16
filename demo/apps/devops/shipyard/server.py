"""Shipyard web app: login, environments page, and the copilot chat.

Knows nothing about hexgate. It loads the copilot from ../agent.py (reloaded
whenever the file changes, so an edit shows up on the next message), calls
`run_copilot(conversation, user, approve)` and streams the run to the browser
as one JSON object per line.

Run: uv run python -m shipyard   (from demo/apps/devops/)
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import secrets
import sys
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from shipyard import infra

APP_DIR = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / "web"
AGENT_FILE = APP_DIR / "agent.py"
USERS_FILE = Path(os.environ.get("SHIPYARD_USERS", APP_DIR / "users.yaml"))

# .env (written by the demo kit) holds the OpenAI and hexgate keys. It never
# overrides what the shell set.
load_dotenv(APP_DIR / ".env")
os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")
try:  # hexgate's tracing uses Langfuse; without Langfuse keys it only warns.
    import langfuse.logger  # noqa: F401  (creates the logger; set its level after)
except ImportError:
    pass
logging.getLogger("langfuse").setLevel(logging.CRITICAL)

log = logging.getLogger("shipyard")
COOKIE = "shipyard_session"


# ---------------------------------------------------------------- users


@dataclass(frozen=True)
class User:
    username: str
    name: str
    roles: tuple[str, ...]


def load_users() -> dict[str, tuple[User, str]]:
    """users.yaml, read on every sign-in so edits apply without a restart."""
    data = yaml.safe_load(USERS_FILE.read_text()) or {}
    users = {}
    for username, u in (data.get("users") or {}).items():
        roles = u.get("roles") or []
        if isinstance(roles, str):
            roles = [roles]
        users[str(username)] = (
            User(str(username), str(u.get("name", username)), tuple(str(r) for r in roles)),
            str(u.get("password", "")),
        )
    return users


_sessions: dict[str, User] = {}
_histories: dict[str, list] = {}  # session token -> conversation items


def current_user(request: Request) -> tuple[str, User]:
    token = request.cookies.get(COOKIE, "")
    user = _sessions.get(token)
    if user is None:
        raise HTTPException(401, "not signed in")
    return token, user


# ---------------------------------------------------------------- the agent module


_agent_mod: Any = None
_agent_mtime = 0.0


def load_agent():
    """Import agent.py, again whenever it changed on disk."""
    global _agent_mod, _agent_mtime
    mtime = AGENT_FILE.stat().st_mtime
    if _agent_mod is None or mtime != _agent_mtime:
        spec = importlib.util.spec_from_file_location("shipyard_copilot", AGENT_FILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if os.environ.get("SHIPYARD_MODEL") == "scripted":
            from shipyard.scripted import ScriptedModel

            mod.copilot.model = ScriptedModel()
        _agent_mod, _agent_mtime = mod, mtime
        log.info("loaded %s (protected=%s)", AGENT_FILE.name, is_protected(mod))
    return _agent_mod


def is_protected(mod) -> bool:
    return "HexgateRunner" in vars(mod)


def agent_status() -> dict:
    try:
        mod = load_agent()
    except Exception as exc:  # a broken edit: show it, don't crash the page
        return {"protected": False, "error": f"{type(exc).__name__}: {exc}"}
    model = getattr(mod.copilot, "model", None)
    return {"protected": is_protected(mod), "error": None, "model": model if isinstance(model, str) else "scripted"}


# ---------------------------------------------------------------- app


app = FastAPI(title="Shipyard")
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index(request: Request):
    if request.cookies.get(COOKIE, "") not in _sessions:
        return RedirectResponse("/login")
    return FileResponse(WEB / "index.html")


@app.get("/login")
def login_page():
    return FileResponse(WEB / "login.html")


@app.get("/api/accounts")
def accounts():
    """Usernames, names and roles for the login page's list. Never passwords."""
    return [
        {"username": u.username, "name": u.name, "roles": list(u.roles)}
        for u, _ in load_users().values()
    ]


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    entry = load_users().get(str(body.get("username", "")).strip())
    if entry is None or not secrets.compare_digest(entry[1], str(body.get("password", ""))):
        raise HTTPException(401, "Wrong username or password.")
    token = secrets.token_urlsafe(24)
    _sessions[token] = entry[0]
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE, "")
    _sessions.pop(token, None)
    _histories.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
def me(request: Request):
    _, user = current_user(request)
    return {"username": user.username, "name": user.name, "roles": list(user.roles), **agent_status()}


@app.get("/api/state")
def state(request: Request):
    current_user(request)
    return {**infra.snapshot(), **agent_status()}


@app.post("/api/chat/reset")
def chat_reset(request: Request):
    token, _ = current_user(request)
    _histories.pop(token, None)
    return {"ok": True}


@app.post("/api/admin/reset")
def admin_reset(request: Request):
    """Back to the start of scene 1: infra state and every chat. Local use only."""
    if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403)
    infra.reset()
    _histories.clear()
    return {"ok": True}


# ---------------------------------------------------------------- chat


_approvals: dict[str, asyncio.Future] = {}


@app.post("/api/approvals/{approval_id}")
async def answer_approval(approval_id: str, request: Request):
    current_user(request)
    body = await request.json()
    fut = _approvals.get(approval_id)
    if fut is None or fut.done():
        raise HTTPException(404, "no pending approval")
    fut.set_result(bool(body.get("approve")))
    return {"ok": True}


def _classify(output: str) -> tuple[str, str]:
    """(status, reason) of a tool result: ok, blocked or rejected."""
    text = str(output)
    if text.startswith("[") and "The tool was not executed" in text:
        status = "rejected" if "requires human approval" in text else "blocked"
        reason = text.split("]", 1)[1].strip()
        for lead in ("is denied by the agent policy", "requires human approval before execution"):
            if lead in reason:
                reason = reason.split(lead, 1)[1].lstrip(":. ").strip()
        reason = reason.replace("The tool was not executed.", "").strip()
        return status, reason
    if text.startswith("An error occurred while running the tool"):
        return "error", text.split("Error:", 1)[-1].strip()
    return "ok", ""


def _args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"input": parsed}
    except (TypeError, ValueError):
        return {"input": raw}


@app.post("/api/chat")
async def chat(request: Request):
    token, user = current_user(request)
    message = str((await request.json()).get("message", "")).strip()
    if not message:
        raise HTTPException(400, "empty message")

    queue: asyncio.Queue = asyncio.Queue()
    put = queue.put_nowait

    async def approve(decision) -> bool:
        approval_id = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        _approvals[approval_id] = fut
        put({
            "type": "approval",
            "id": approval_id,
            "tool": decision.tool_name,
            "args": decision.arguments or {},
            "reason": decision.reason or "",
        })
        try:
            ok = await fut
        finally:
            _approvals.pop(approval_id, None)
        put({"type": "approval_done", "id": approval_id, "approved": ok})
        infra.log_event(
            "ok" if ok else "blocked",
            f"{decision.tool_name} {'approved' if ok else 'rejected'} for {user.name}",
            user.username,
        )
        return ok

    async def produce() -> None:
        history = _histories.get(token, [])
        conversation = history + [{"role": "user", "content": message}]
        tool_names: dict[str, tuple[str, dict]] = {}
        actor_token = infra.actor.set(user.username)
        try:
            mod = load_agent()
            result = await mod.run_copilot(conversation, user, approve)
            async for ev in result.stream_events():
                if ev.type == "raw_response_event":
                    if getattr(ev.data, "type", "") == "response.output_text.delta":
                        put({"type": "text", "delta": ev.data.delta})
                elif ev.type == "run_item_stream_event":
                    item = ev.item
                    if ev.name == "tool_called":
                        raw = item.raw_item
                        call_id = getattr(raw, "call_id", None) or getattr(raw, "id", "")
                        name = getattr(raw, "name", "tool")
                        args = _args(getattr(raw, "arguments", "{}"))
                        tool_names[call_id] = (name, args)
                        put({"type": "tool_start", "id": call_id, "name": name, "args": args})
                    elif ev.name == "tool_output":
                        raw = item.raw_item
                        call_id = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", "")
                        status, reason = _classify(item.output)
                        name, args = tool_names.get(call_id, ("tool", {}))
                        if status != "ok":
                            pretty = ", ".join(str(v) for v in args.values())
                            infra.log_event("blocked", f"Blocked: {name}({pretty})", user.username)
                        put({
                            "type": "tool_end",
                            "id": call_id,
                            "status": status,
                            "reason": reason,
                            "output": str(item.output)[:600],
                        })
            _histories[token] = result.to_input_list()
        except Exception as exc:
            log.warning("copilot run failed: %s", exc)
            traceback.print_exc()
            put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            infra.actor.reset(actor_token)
            put({"type": "done"})

    task = asyncio.create_task(produce())

    async def stream():
        try:
            while True:
                ev = await queue.get()
                yield json.dumps(ev) + "\n"
                if ev["type"] == "done":
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("SHIPYARD_PORT", "3000"))
    try:
        load_agent()
    except Exception as exc:
        print(f"warning: agent.py does not load yet: {exc}", file=sys.stderr)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
