"""Tellerdesk web app: login, the customer file, and the assistant chat.

Knows nothing about hexgate. It loads the assistant from ../agent.py (reloaded
whenever the file changes, so an edit shows up on the next message), calls
`run_assistant(conversation, user, approve)` and streams the run to the
browser as one JSON object per line. The user it passes carries, as
attributes, the customer file open in that session's page.

Run: uv run python -m tellerdesk   (from demo/apps/bank/)
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
import types
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from tellerdesk import bank

APP_DIR = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / "web"
AGENT_FILE = APP_DIR / "agent.py"
USERS_FILE = Path(os.environ.get("TELLERDESK_USERS", APP_DIR / "users.yaml"))
DEFAULT_PORT = "3002"
SHOWN_OUTPUT_CHARS = (
    4000  # a whole statement: the chat can show what the assistant read
)
SHOWN_ARG_CHARS = 40

# .env (written by the demo kit) holds the OpenAI and hexgate keys. It never
# overrides what the shell set.
load_dotenv(APP_DIR / ".env")
# Except the hexgate settings: the kit writes this project's key and URL there,
# and a HEXGATE_API_KEY exported for another project must not win.
for _k, _v in dotenv_values(APP_DIR / ".env").items():
    if _k.startswith("HEXGATE_") and _v:
        os.environ[_k] = _v
os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")
try:  # hexgate's tracing uses Langfuse; without Langfuse keys it only warns.
    import langfuse.logger  # noqa: F401  (creates the logger; set its level after)
except ImportError:
    pass
logging.getLogger("langfuse").setLevel(logging.CRITICAL)

log = logging.getLogger("tellerdesk")
COOKIE = "tellerdesk_session"


# ---------------------------------------------------------------- users


@dataclass(frozen=True)
class User:
    username: str
    name: str
    roles: tuple[str, ...]
    attributes: Mapping[str, Any] = field(
        default_factory=lambda: types.MappingProxyType({})
    )

    def public(self) -> dict:
        """What the pages may show. Never the password."""
        return {
            "username": self.username,
            "name": self.name,
            "roles": list(self.roles),
            "attributes": dict(self.attributes),
        }


def load_users() -> dict[str, tuple[User, str]]:
    """users.yaml, read on every sign-in so edits apply without a restart."""
    data = yaml.safe_load(USERS_FILE.read_text()) or {}
    users = {}
    for username, u in (data.get("users") or {}).items():
        roles = u.get("roles") or []
        if isinstance(roles, str):
            roles = [roles]
        users[str(username)] = (
            User(
                str(username),
                str(u.get("name", username)),
                tuple(str(r) for r in roles),
                types.MappingProxyType(dict(u.get("attributes") or {})),
            ),
            str(u.get("password", "")),
        )
    return users


_sessions: dict[str, User] = {}
_histories: dict[str, list] = {}  # session token -> conversation items
_open_customer: dict[str, str] = {}  # session token -> customer id open in the page
_chat_generations: dict[str, int] = {}  # token -> bumped when its chat starts over


def chat_generation(token: str) -> int:
    return _chat_generations.get(token, 0)


def start_chat_over(token: str) -> None:
    _histories.pop(token, None)
    _chat_generations[token] = chat_generation(token) + 1


def is_current_chat(token: str, generation: int) -> bool:
    """A run may save its history only if nothing started the chat over meanwhile."""
    return token in _sessions and chat_generation(token) == generation


def open_customer_of(token: str) -> str:
    return _open_customer.get(token, bank.DEFAULT_CUSTOMER)


def with_open_file(user: User, customer_id: str) -> User:
    """The user as the assistant sees them: their own attributes plus the open customer file."""
    attributes = {**user.attributes, **bank.session_attributes(customer_id)}
    return replace(user, attributes=types.MappingProxyType(attributes))


def current_user(request: Request) -> tuple[str, User]:
    token = request.cookies.get(COOKIE, "")
    user = _sessions.get(token)
    if user is None:
        raise HTTPException(401, "not signed in")
    return token, user


# ---------------------------------------------------------------- the agent module


_agent_mod: Any = None
_agent_mtime = 0.0


def load_agent() -> types.ModuleType:
    """Import agent.py, again whenever it changed on disk."""
    global _agent_mod, _agent_mtime
    mtime = AGENT_FILE.stat().st_mtime
    if _agent_mod is None or mtime != _agent_mtime:
        spec = importlib.util.spec_from_file_location(
            "tellerdesk_assistant", AGENT_FILE
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if os.environ.get("TELLERDESK_MODEL") == "scripted":
            from tellerdesk.scripted import ScriptedModel

            mod.assistant.model = ScriptedModel()
        _agent_mod, _agent_mtime = mod, mtime
        log.info("loaded %s (protected=%s)", AGENT_FILE.name, is_protected(mod))
    return _agent_mod


def is_protected(mod: types.ModuleType) -> bool:
    """agent.py holds something from hexgate (a runner, its adapter module, an
    alias), whatever the import style. The badge only says hexgate is in play;
    the runs themselves are what the policy checks."""
    for value in vars(mod).values():
        origin = (
            value.__name__
            if isinstance(value, types.ModuleType)
            else getattr(value, "__module__", None)
        )
        if isinstance(origin, str) and (
            origin == "hexgate" or origin.startswith("hexgate.")
        ):
            return True
    return False


def agent_status() -> dict:
    try:
        mod = load_agent()
    except Exception as exc:  # a broken edit: show it, don't crash the page
        return {"protected": False, "error": f"{type(exc).__name__}: {exc}"}
    model = getattr(getattr(mod, "assistant", None), "model", None)
    return {
        "protected": is_protected(mod),
        "error": None,
        "model": model if isinstance(model, str) else "scripted",
    }


# ---------------------------------------------------------------- app


app = FastAPI(title="Tellerdesk")
app.mount("/static", StaticFiles(directory=WEB, check_dir=False), name="static")


@app.get("/")
def index(request: Request) -> Response:
    if request.cookies.get(COOKIE, "") not in _sessions:
        return RedirectResponse("/login")
    return FileResponse(WEB / "index.html")


@app.get("/login")
def login_page() -> Response:
    return FileResponse(WEB / "login.html")


@app.get("/api/accounts")
def accounts() -> list[dict]:
    """Usernames, names, roles and attributes for the login page's list."""
    return [u.public() for u, _ in load_users().values()]


@app.post("/api/login")
async def login(request: Request) -> Response:
    body = await request.json()
    entry = load_users().get(str(body.get("username", "")).strip())
    if entry is None or not secrets.compare_digest(
        entry[1].encode(), str(body.get("password", "")).encode()
    ):
        raise HTTPException(401, "Wrong username or password.")
    token = secrets.token_urlsafe(24)
    _sessions[token] = entry[0]
    _open_customer[token] = bank.DEFAULT_CUSTOMER
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
def logout(request: Request) -> Response:
    token = request.cookies.get(COOKIE, "")
    _sessions.pop(token, None)
    _histories.pop(token, None)
    _open_customer.pop(token, None)
    _chat_generations.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
def me(request: Request) -> dict:
    token, user = current_user(request)
    return {**user.public(), "open_customer": open_customer_of(token), **agent_status()}


@app.get("/api/state")
def state(request: Request) -> dict:
    token, _ = current_user(request)
    return {
        **bank.snapshot(),
        "open_customer": open_customer_of(token),
        **agent_status(),
    }


@app.post("/api/session")
async def open_file(request: Request) -> dict:
    """Open another customer's file. The chat starts over: one customer's
    conversation never carries into another's file."""
    token, _ = current_user(request)
    customer_id = str((await request.json()).get("customer_id", "")).strip()
    if not bank.is_customer(customer_id):
        raise HTTPException(404, f"unknown customer {customer_id!r}")
    if customer_id != open_customer_of(token):
        start_chat_over(token)
    _open_customer[token] = customer_id
    return {"ok": True, "open_customer": customer_id}


@app.post("/api/chat/reset")
def chat_reset(request: Request) -> dict:
    token, _ = current_user(request)
    start_chat_over(token)
    return {"ok": True}


@app.post("/api/admin/reset")
def admin_reset(request: Request) -> dict:
    """Back to the start of scene 1: bank state, every chat, every open file. Local use only."""
    if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403)
    bank.reset()
    for token in list(_sessions):
        start_chat_over(token)
    _histories.clear()
    _open_customer.clear()
    return {"ok": True}


# ---------------------------------------------------------------- chat


_approvals: dict[str, asyncio.Future] = {}


@app.post("/api/approvals/{approval_id}")
async def answer_approval(approval_id: str, request: Request) -> dict:
    current_user(request)
    body = await request.json()
    fut = _approvals.get(approval_id)
    if fut is None or fut.done():
        raise HTTPException(404, "no pending approval")
    fut.set_result(bool(body.get("approve")))
    return {"ok": True}


def _classify(output: str) -> tuple[str, str]:
    """(status, reason) of a tool result: ok, blocked, rejected or error."""
    text = str(output)
    if text.startswith("[") and "The tool was not executed" in text:
        status = "rejected" if "requires human approval" in text else "blocked"
        reason = text.split("]", 1)[1].strip()
        for lead in (
            "is denied by the agent policy",
            "requires human approval before execution",
        ):
            if lead in reason:
                reason = reason.split(lead, 1)[1].lstrip(":. ").strip()
        reason = reason.replace("The tool was not executed.", "").strip()
        return status, reason
    if text.startswith("An error occurred while running the tool"):
        return "error", text.split("Error:", 1)[-1].strip()
    if text.startswith(bank.ERROR_PREFIX):
        return "error", text.removeprefix(bank.ERROR_PREFIX).strip()
    return "ok", ""


def _args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"input": parsed}
    except (TypeError, ValueError):
        return {"input": raw}


def _brief(value: Any) -> str:
    """An argument as the activity feed shows it: one line, cut short (an email body)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    line = str(value).split("\n", 1)[0]
    return line if len(line) <= SHOWN_ARG_CHARS else line[:SHOWN_ARG_CHARS] + "…"


@app.post("/api/chat")
async def chat(request: Request) -> StreamingResponse:
    token, user = current_user(request)
    message = str((await request.json()).get("message", "")).strip()
    if not message:
        raise HTTPException(400, "empty message")

    queue: asyncio.Queue = asyncio.Queue()
    put = queue.put_nowait

    async def approve(decision: Any) -> bool:
        approval_id = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        _approvals[approval_id] = fut
        arguments = decision.arguments or {}
        put(
            {
                "type": "approval",
                "id": approval_id,
                "tool": decision.tool_name,
                "args": arguments,
                "reason": decision.reason or "",
            }
        )
        try:
            ok = await fut
        finally:
            _approvals.pop(approval_id, None)
        put({"type": "approval_done", "id": approval_id, "approved": ok})
        bank.log_event(
            "ok" if ok else "blocked",
            f"{decision.tool_name} {'approved' if ok else 'rejected'} for {user.name}",
            user.username,
        )
        return ok

    async def produce() -> None:
        history = _histories.get(token, [])
        conversation = history + [{"role": "user", "content": message}]
        tool_names: dict[str, tuple[str, dict]] = {}
        customer_id = open_customer_of(token)
        generation = chat_generation(token)
        actor_token = bank.actor.set(user.username)
        customer_token = bank.open_customer.set(customer_id)
        try:
            mod = load_agent()
            result = await mod.run_assistant(
                conversation, with_open_file(user, customer_id), approve
            )
            async for ev in result.stream_events():
                if ev.type == "raw_response_event":
                    if getattr(ev.data, "type", "") == "response.output_text.delta":
                        put({"type": "text", "delta": ev.data.delta})
                elif ev.type == "run_item_stream_event":
                    item = ev.item
                    if ev.name == "tool_called":
                        raw = item.raw_item
                        call_id = getattr(raw, "call_id", None) or getattr(
                            raw, "id", ""
                        )
                        name = getattr(raw, "name", "tool")
                        args = _args(getattr(raw, "arguments", "{}"))
                        tool_names[call_id] = (name, args)
                        put(
                            {
                                "type": "tool_start",
                                "id": call_id,
                                "name": name,
                                "args": args,
                            }
                        )
                    elif ev.name == "tool_output":
                        raw = item.raw_item
                        call_id = (
                            raw.get("call_id")
                            if isinstance(raw, dict)
                            else getattr(raw, "call_id", "")
                        )
                        status, reason = _classify(item.output)
                        name, args = tool_names.get(call_id, ("tool", {}))
                        if status in ("blocked", "rejected"):
                            pretty = ", ".join(_brief(v) for v in args.values())
                            bank.log_event(
                                "blocked", f"Blocked: {name}({pretty})", user.username
                            )
                        put(
                            {
                                "type": "tool_end",
                                "id": call_id,
                                "status": status,
                                "reason": reason,
                                "output": str(item.output)[:SHOWN_OUTPUT_CHARS],
                            }
                        )
            if is_current_chat(token, generation):
                _histories[token] = result.to_input_list()
        except Exception as exc:
            log.warning("assistant run failed: %s", exc)
            traceback.print_exc()
            put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            bank.actor.reset(actor_token)
            bank.open_customer.reset(customer_token)
            put({"type": "done"})

    task = asyncio.create_task(produce())

    async def stream() -> AsyncIterator[str]:
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

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    port = int(os.environ.get("TELLERDESK_PORT", DEFAULT_PORT))
    try:
        load_agent()
    except Exception as exc:
        print(f"warning: agent.py does not load yet: {exc}", file=sys.stderr)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
