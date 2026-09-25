"""Serve a Hexgate-wrapped OpenAI agent as a watsonx Orchestrate external agent.

Orchestrate calls ``POST /chat/completions`` (its ``external_chat`` provider);
each turn runs through ``HexgateRunner`` so every tool call is policy-gated, and
the normalized Hexgate stream is re-emitted in Orchestrate's SSE format
(see ``sse.py``). Run from the repo root::

    uvicorn examples.watsonx_orchestrate.app:app --host 0.0.0.0 --port 8080

Environment:

* ``HEXGATE_API_KEY`` — Hexgate platform key (policy pull, audit).
* ``OPENAI_API_KEY`` — the agent's LLM.
* ``HEXGATE_ORCHESTRATE_TOKEN`` — shared secret Orchestrate must send, as
  ``Authorization: Bearer <token>`` or ``x-api-key: <token>``. We generate it
  (``openssl rand -hex 32``); IBM does not issue it.
* ``HEXGATE_ORCHESTRATE_USER_ROLES`` — comma-separated roles every call runs as
  (default ``operator``). Orchestrate's documented request carries no end-user
  identity, so roles are fixed per deployment for now.
* ``HEXGATE_ORCHESTRATE_USER_ID`` — user id recorded on the run (default
  ``watsonx-orchestrate``).
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from examples.watsonx_orchestrate.sse import (
    DONE,
    completion_body,
    last_user_message,
    to_agent_input,
    to_sse,
)
from hexgate.config.env import resolve_api_key
from hexgate.runtime import HexgateContext
from hexgate.streaming import ErrorEvent, RunEndEvent, StreamEvent

# Child of uvicorn's logger so the request-shape lines show at uvicorn's INFO level.
logger = logging.getLogger("uvicorn.error.hexgate_orchestrate")

# (agent input items, context, query) -> normalized Hexgate events.
StreamFn = Callable[
    [list[dict[str, str]], HexgateContext, str], AsyncIterator[StreamEvent]
]

# Placeholder key for local-policy runs: HexgateRunner insists on a key, but with
# HEXGATE_LOCAL_POLICY + HEXGATE_LOCAL_MODE the policy comes from the file and
# nothing is sent to the platform, so the value is never used.
_LOCAL_PLACEHOLDER_KEY = "local-policy"


def _hexgate_stream() -> tuple[StreamFn, str]:
    """Build the production stream function around ``HexgateRunner``."""
    from examples.watsonx_orchestrate.agent import agent
    from hexgate.adapters.openai import HexgateRunner
    from hexgate.adapters.openai.streaming import normalize_openai_events

    api_key = None
    if os.environ.get("HEXGATE_LOCAL_POLICY") and not resolve_api_key():
        # Set before the runner is built so audit/ban/span senders stay inert.
        os.environ.setdefault("HEXGATE_LOCAL_MODE", "1")
        api_key = _LOCAL_PLACEHOLDER_KEY
    runner = HexgateRunner(api_key)  # without a key or local policy: fails loud

    async def stream(items, ctx, query):
        result = await runner.arun_streamed(agent, items, hexgate_context=ctx)
        try:
            async for event in normalize_openai_events(
                result.stream_events(), query=query
            ):
                yield event
        finally:
            # Orchestrate hung up (or the run failed): stop the background agent
            # task so it doesn't keep calling the LLM and running tools unobserved.
            result.cancel()

    return stream, agent.name


def _authorized(expected: str, authorization: str | None, api_key: str | None) -> bool:
    """True when the bearer token or the ``x-api-key`` header matches ``expected``."""
    candidates = [api_key]
    if authorization and authorization.lower().startswith("bearer "):
        candidates.append(authorization[len("bearer ") :].strip())
    # Compare bytes: compare_digest rejects non-ASCII str with a TypeError.
    want = expected.encode()
    return any(c and secrets.compare_digest(c.encode(), want) for c in candidates)


async def _read_body(request: Request) -> dict[str, Any]:
    """Parse and shape-check the request body; 400 on anything malformed."""
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body is not valid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    if not isinstance(body.get("messages"), list):
        raise HTTPException(status_code=400, detail="messages must be a list")
    return body


def create_app(
    *,
    stream: StreamFn | None = None,
    model: str | None = None,
    token: str | None = None,
    user_id: str | None = None,
    user_roles: list[str] | None = None,
) -> FastAPI:
    """Build the bridge app. Arguments override the environment (used by tests).

    Reads ``os.environ`` only; loading ``.env`` is the entrypoint's job (see
    ``__getattr__`` below), so tests that inject everything leak nothing.
    """
    token = token or os.environ.get("HEXGATE_ORCHESTRATE_TOKEN")
    if not token:
        raise RuntimeError(
            "HEXGATE_ORCHESTRATE_TOKEN is not set. Generate one with "
            "`openssl rand -hex 32` and give the same value to Orchestrate."
        )
    if stream is None:
        stream, agent_name = _hexgate_stream()
        model = model or agent_name
    model = model or "hexgate-agent"
    user_id = user_id or os.environ.get(
        "HEXGATE_ORCHESTRATE_USER_ID", "watsonx-orchestrate"
    )
    if user_roles is None:
        raw = os.environ.get("HEXGATE_ORCHESTRATE_USER_ROLES", "operator")
        user_roles = [r.strip() for r in raw.split(",") if r.strip()]

    app = FastAPI(title="Hexgate × watsonx Orchestrate bridge")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "agent": model}

    @app.post("/chat/completions")
    async def chat_completions(
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
        x_ibm_thread_id: str | None = Header(None),
    ) -> Any:
        if not _authorized(token, authorization, x_api_key):
            raise HTTPException(status_code=401, detail="invalid or missing token")

        body = await _read_body(request)
        messages = body["messages"]
        extra_body = body.get("extra_body")
        body_thread = (
            extra_body.get("thread_id") if isinstance(extra_body, dict) else None
        )
        thread_id = x_ibm_thread_id or body_thread or str(uuid.uuid4())
        # Log the request *shape* (never values or the token): this bridge is an
        # experiment, and what Orchestrate actually forwards (context variables,
        # identity) is still an open question.
        logger.info(
            "orchestrate call thread=%s stream=%s body_keys=%s headers=%s",
            thread_id,
            body.get("stream"),
            sorted(body.keys()),
            sorted(
                h
                for h in request.headers.keys()
                if h not in ("authorization", "x-api-key")
            ),
        )

        query = last_user_message(messages)
        if not query:
            raise HTTPException(
                status_code=400, detail="the latest user message has no text"
            )
        items = to_agent_input(messages)
        ctx = HexgateContext(
            user_id=user_id, session_id=thread_id, user_roles=user_roles
        )

        if body.get("stream"):

            async def sse() -> AsyncIterator[str]:
                try:
                    # aclosing: when Orchestrate disconnects, close the inner
                    # stream now (running its cancel-the-run cleanup), not at GC.
                    async with contextlib.aclosing(stream(items, ctx, query)) as events:
                        async for event in events:
                            frame = to_sse(event, thread_id=thread_id, model=model)
                            if frame is not None:
                                yield frame
                # Surface setup failures (unregistered agent, ban…) to the chat.
                except Exception as exc:
                    logger.exception("orchestrate stream failed")
                    error = ErrorEvent(
                        run_id=thread_id,
                        root_run_id=thread_id,
                        sequence=0,
                        message=str(exc),
                    )
                    yield to_sse(error, thread_id=thread_id, model=model)
                yield DONE

            return StreamingResponse(sse(), media_type="text/event-stream")

        message = ""
        errors: list[str] = []
        try:
            async for event in stream(items, ctx, query):
                if isinstance(event, RunEndEvent):
                    message = event.result.message
                elif isinstance(event, ErrorEvent):
                    errors.append(f"[error] {event.message}")
        except Exception as exc:
            logger.exception("orchestrate run failed")
            errors.append(f"[error] {exc}")
        content = "\n\n".join(part for part in (message, *errors) if part)
        return JSONResponse(completion_body(content, model=model))

    return app


def __getattr__(name: str) -> Any:
    # Build lazily so importing this module (tests, tooling) needs no keys;
    # uvicorn's `examples.watsonx_orchestrate.app:app` lookup triggers it.
    if name == "app":
        from hexgate.bootstrap import bootstrap

        bootstrap()  # loads ./.env (cwd only, shell wins) and configures audit
        global app
        app = create_app()
        return app
    raise AttributeError(name)
