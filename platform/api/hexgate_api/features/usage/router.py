"""SDK-facing read of ``agent_usage.*`` windows; the project comes from the bearer."""

import asyncio
import logging
from datetime import datetime
from typing import Final

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.db import get_session
from hexgate_api.deps.clickhouse import _audit_unavailable, require_clickhouse
from hexgate_api.deps.tokens import require_project
from hexgate_api.features.agents.service import get_agent
from hexgate_api.features.usage.paths import InvalidUsagePaths, parse_usage_paths
from hexgate_api.features.usage.service import (
    USAGE_MEMO_TTL_SECONDS,
    USAGE_READ_TIMEOUT_SECONDS,
    UsageMemo,
    get_usage_memo,
    read_usage,
)

_log = logging.getLogger(__name__)

router = APIRouter()

CACHE_CONTROL: Final = f"private, max-age={int(USAGE_MEMO_TTL_SECONDS)}"
_AGENT_NOT_FOUND: Final = "agent not found"


class AgentUsageRead(BaseModel):
    """``GET /v1/agents/{name}/usage``. ``values`` is keyed by the paths as requested."""

    as_of: datetime
    values: dict[str, int]


@router.get("/agents/{name}/usage", response_model=AgentUsageRead, tags=["usage"])
async def api_get_agent_usage(
    name: str,
    response: Response,
    paths: str = Query(...),
    project_id: str = Depends(require_project),
    session: AsyncSession = Depends(get_session),
    clickhouse_client: Client = Depends(require_clickhouse),
    memo: UsageMemo = Depends(get_usage_memo),
) -> AgentUsageRead:
    """SDK read of agent_usage.* for the bearer's project. 404 if the agent isn't
    registered there, 422 on a bad path, 503 when ClickHouse is unavailable."""
    try:
        requested = parse_usage_paths(paths)
    except InvalidUsagePaths as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if await get_agent(session, project_id, name) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_AGENT_NOT_FOUND
        )

    specs = tuple(sorted(set(requested.values())))
    try:
        readout = await memo.get_or_load(
            (project_id, name, specs),
            # Inside the loader, so a stalled read fails the shared task (and isn't
            # memoized) rather than releasing one caller.
            lambda: asyncio.wait_for(
                asyncio.to_thread(
                    read_usage, clickhouse_client, project_id, name, specs
                ),
                timeout=USAGE_READ_TIMEOUT_SECONDS,
            ),
        )
    except (ClickHouseError, TimeoutError) as exc:
        _log.warning("usage read failed for agent %r: %s", name, exc)
        raise _audit_unavailable() from exc

    response.headers["Cache-Control"] = CACHE_CONTROL
    return AgentUsageRead(
        as_of=readout.as_of,
        values={path: readout.values[spec] for path, spec in requested.items()},
    )
