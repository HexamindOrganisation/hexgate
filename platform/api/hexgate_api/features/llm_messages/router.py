"""LLM message read endpoint: one transcript for the Audit drawer.

Read-only. Messages have no HTTP ingest — the OTLP pipeline writes them
through the enricher — so this router is the whole HTTP surface of the
slice, cookie-authed like the other project-scoped dashboard reads.
"""

import asyncio
from uuid import UUID

from clickhouse_connect.driver.exceptions import ClickHouseError
from fastapi import APIRouter, Depends, HTTPException, Query

from hexgate_api.deps.clickhouse import _audit_unavailable, require_clickhouse
from hexgate_api.deps.org import require_org_member
from hexgate_api.features.llm_messages.service import (
    MAX_PAGE_SIZE,
    NoMessageScope,
    list_llm_messages,
)
from hexgate_api.schemas import LlmMessagePage

router = APIRouter()


def _parsed_run_id(raw: str | None) -> UUID | None:
    """``None`` for absent or blank, a UUID otherwise; ValueError if malformed.

    A blank reads as absent rather than as an error so that a caller echoing a
    decision row whose run_id was null still gets its session scope.
    """
    if not raw:
        return None
    return UUID(raw)


# No window / date-range parameters, unlike the other project-scoped reads:
# the transcript is the scope, and a time filter on top of it could only cut
# the head off a conversation (see ``list_llm_messages``). Long transcripts
# are taken in pages.
@router.get(
    "/projects/{project_id}/audit/llm-messages",
    response_model=LlmMessagePage,
    dependencies=[Depends(require_org_member)],
    tags=["llm_messages"],
)
async def api_llm_messages(
    project_id: str,
    # Either scope, or both; neither is a 422 — see ``list_llm_messages``.
    # Both arrive as the caller sent them, blanks included: a drawer forwarding
    # a decision row sends whatever that row held, and an empty field there is
    # data, not a malformed request. ``run_id`` is parsed by hand for that
    # reason — annotating it ``UUID`` would 422 a blank before the scope check,
    # the mirror of the constraint removed from session_id in fe323177.
    session_id: str | None = Query(
        default=None,
        description=(
            "Session to read the transcript of. Send it EMPTY (`?session_id=`) "
            "rather than omitting it when the decision you are reading from has "
            "no session id — an empty value is the fast path, because session_id "
            "is the second column of the storage sort key and pinning it, even "
            'to "", keeps the scan inside one contiguous block. Omitting it '
            'entirely means "I do not know the session" and makes a run-scoped '
            "read scan every session in the project. Empty on its own is not a "
            "scope; pair it with run_id."
        ),
    ),
    run_id: str | None = Query(
        default=None,
        description=(
            "Run to read the transcript of, for the common case of an SDK user "
            "who never set a session id. A blank value reads as absent, so a "
            "caller may forward a decision row's null run_id unchanged."
        ),
    ),
    limit: int = 50,
    offset: int = 0,
    clickhouse_client=Depends(require_clickhouse),
) -> LlmMessagePage:
    try:
        parsed_run_id = _parsed_run_id(run_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="run_id is not a valid UUID")

    try:
        # The clickhouse_connect client is sync — run it off the event loop so
        # a slow scan can't stall every other in-flight request.
        page = await asyncio.to_thread(
            list_llm_messages,
            clickhouse_client,
            project_id=project_id,
            session_id=session_id,
            run_id=parsed_run_id,
            limit=max(1, min(limit, MAX_PAGE_SIZE)),
            offset=max(0, offset),
        )
    except NoMessageScope as exc:
        # 422, matching what FastAPI returns for a malformed query parameter:
        # the request named no transcript, so there is nothing to serve.
        raise HTTPException(status_code=422, detail=str(exc))
    except ClickHouseError:
        raise _audit_unavailable()
    return page
