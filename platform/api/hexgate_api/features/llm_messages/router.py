"""LLM message read endpoint: one transcript for the Audit drawer.

Read-only. Messages have no HTTP ingest — the OTLP pipeline writes them
through the enricher — so this router is the whole HTTP surface of the
slice, cookie-authed like the other project-scoped dashboard reads.
"""

import asyncio
from uuid import UUID

from clickhouse_connect.driver.exceptions import ClickHouseError
from fastapi import APIRouter, Depends, HTTPException

from hexgate_api.deps.clickhouse import _audit_unavailable, require_clickhouse
from hexgate_api.deps.org import require_org_member
from hexgate_api.features.llm_messages.service import (
    MAX_PAGE_SIZE,
    NoMessageScope,
    list_llm_messages,
)
from hexgate_api.schemas import LlmMessagePage

router = APIRouter()


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
    session_id: str | None = None,
    run_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
    clickhouse_client=Depends(require_clickhouse),
) -> LlmMessagePage:
    try:
        # The clickhouse_connect client is sync — run it off the event loop so
        # a slow scan can't stall every other in-flight request.
        page = await asyncio.to_thread(
            list_llm_messages,
            clickhouse_client,
            project_id=project_id,
            session_id=session_id,
            run_id=run_id,
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
