"""LLM message read endpoint: one session's transcript for the Audit drawer.

Read-only. Messages have no HTTP ingest — the OTLP pipeline writes them
through the enricher — so this router is the whole HTTP surface of the
slice, cookie-authed like the other project-scoped dashboard reads.
"""

import asyncio

from clickhouse_connect.driver.exceptions import ClickHouseError
from fastapi import APIRouter, Depends, Query

from hexgate_api.deps.clickhouse import _audit_unavailable, require_clickhouse
from hexgate_api.deps.org import require_org_member
from hexgate_api.features.llm_messages.service import MAX_PAGE_SIZE, list_llm_messages
from hexgate_api.schemas import LlmMessagePage

router = APIRouter()


# No window / date-range parameters, unlike the other project-scoped reads:
# the session is the scope, and a time filter on top of it could only cut the
# head off a conversation (see ``list_llm_messages``). Long transcripts are
# taken in pages.
@router.get(
    "/projects/{project_id}/audit/llm-messages",
    response_model=LlmMessagePage,
    dependencies=[Depends(require_org_member)],
    tags=["llm_messages"],
)
async def api_llm_messages(
    project_id: str,
    # Required and non-empty: the transcript is always read for one session.
    # An optional session_id would make a forgotten (or blanked) parameter a
    # full-project dump of every stored prompt rather than a 422.
    session_id: str = Query(min_length=1),
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
            limit=max(1, min(limit, MAX_PAGE_SIZE)),
            offset=max(0, offset),
        )
    except ClickHouseError:
        raise _audit_unavailable()
    return page
