"""The turn route: ``POST /v1/assistant/projects/{project_id}/turns``.

A turn that will not run is refused with plain HTTP before any stream opens
in this order: the token, the model, the body, then the user's lock, the
session and the turn-start reads. Once it will run, the answer is an AG-UI
event stream.
"""

import logging
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from hexgate_assistant import runner
from hexgate_assistant.auth import authenticate
from hexgate_assistant.context import Editor, ProjectUnreadable
from hexgate_assistant.lock import TurnBusy
from hexgate_assistant.sessions import SessionNotFound

logger = logging.getLogger(__name__)


class EditorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open_file: str | None = Field(default=None, max_length=512)
    unsaved: list[str] = Field(default_factory=list, max_length=50)


class TurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=8000)
    editor: EditorBody = Field(default_factory=EditorBody)


async def post_turn(request: Request) -> Response:
    project_id = request.path_params["project_id"]
    token = await authenticate(request, request.app.state.verifier, project_id)
    turns: runner.Turns | None = request.app.state.turns
    if turns is None:
        return _refusal(503, "disabled")
    try:
        body = TurnBody.model_validate_json(await request.body())
    except ValidationError as exc:
        return JSONResponse({"detail": exc.errors(include_input=False)}, 422)
    turn = runner.TurnRequest(
        session_id=body.session_id,
        message=body.message,
        editor=Editor(body.editor.open_file, tuple(body.editor.unsaved)),
    )
    try:
        started = await runner.start(turns, token, project_id, turn)
    except TurnBusy:
        return _refusal(409, "busy")
    except SessionNotFound:
        raise HTTPException(404, "session not found") from None
    except ProjectUnreadable as exc:
        logger.warning("turn of %s: project unreadable: %s", token.subject, exc)
        raise HTTPException(502, "the platform could not be reached") from None
    return StreamingResponse(
        runner.stream(started),
        media_type="text/event-stream",
        # No proxy or browser cache holds back an event.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _refusal(status: int, code: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code}}, status)
