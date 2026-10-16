"""SDK-facing read of ``agent_usage.*`` and ``user_usage.*`` windows; the project
comes from the bearer."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Final

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.db import get_session
from hexgate_api.deps.clickhouse import clickhouse_getter, clickhouse_unavailable
from hexgate_api.deps.tokens import require_project
from hexgate_api.features.agents.service import get_agent
from hexgate_api.features.usage.paths import (
    InvalidUsagePaths,
    UsageWindowSpec,
    parse_usage_path_list,
    parse_usage_paths,
)
from hexgate_api.features.usage.service import (
    AGENT_ROLLUP,
    USAGE_MEMO_TTL_SECONDS,
    USAGE_READ_TIMEOUT_SECONDS,
    USER_ROLLUP,
    UsageMemo,
    UsageReadout,
    UsageRollup,
    get_usage_executor,
    get_usage_memo,
    get_user_usage_executor,
    get_user_usage_memo,
    read_usage,
)
from hexgate_api.features.usage.stats import (
    UsageReadStats,
    get_agent_read_stats,
    get_user_read_stats,
)

_log = logging.getLogger(__name__)

router = APIRouter()

CACHE_CONTROL: Final = f"private, max-age={int(USAGE_MEMO_TTL_SECONDS)}"
# The body is keyed by personal data: no intermediary may keep it.
USER_USAGE_CACHE_CONTROL: Final = "private, no-store"
USER_ID_MAX_BYTES: Final = 256
_USER_ID_ENCODING: Final = "utf-8"
_AGENT_NOT_FOUND: Final = "agent not found"
_USAGE_UNAVAILABLE: Final = "usage temporarily unavailable"
_USER_ID_EMPTY: Final = "user_id is empty"
_USER_ID_TOO_LONG: Final = f"user_id is longer than {USER_ID_MAX_BYTES} bytes"
_USER_ID_NOT_UTF8: Final = "user_id is not valid UTF-8"


class UsageRead(BaseModel):
    """``values`` is keyed by the paths as requested."""

    as_of: datetime
    values: dict[str, int]


class UserUsageQuery(BaseModel):
    """``POST /v1/usage/users:read`` body. user_id goes in the body, never the URL
    (README G10). An object, so a batch or a verified flag can be added later."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    paths: list[str]


@router.get("/agents/{name}/usage", response_model=UsageRead, tags=["usage"])
async def api_get_agent_usage(
    name: str,
    response: Response,
    paths: str = Query(...),
    project_id: str = Depends(require_project),
    session: AsyncSession = Depends(get_session),
    get_client: Callable[[], Client] = Depends(clickhouse_getter),
    memo: UsageMemo = Depends(get_usage_memo),
    executor: ThreadPoolExecutor = Depends(get_usage_executor),
    stats: UsageReadStats = Depends(get_agent_read_stats),
) -> UsageRead:
    """SDK read of agent_usage.* for the bearer's project. 404 if the agent isn't
    registered there, 422 on a bad path, 503 when ClickHouse is unavailable."""
    requested = _parse_or_422(parse_usage_paths, paths)
    if await get_agent(session, project_id, name) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_AGENT_NOT_FOUND
        )
    # Hand the pooled connection back rather than hold it across the ClickHouse wait.
    await session.rollback()

    read = await _read_windows(
        rollup=AGENT_ROLLUP,
        project_id=project_id,
        key=name,
        requested=requested,
        get_client=get_client,
        memo=memo,
        executor=executor,
        stats=stats,
        failure_subject=f"agent {name!r} in project {project_id}",
    )
    response.headers["Cache-Control"] = CACHE_CONTROL
    return read


@router.post("/usage/users:read", response_model=UsageRead, tags=["usage"])
async def api_read_user_usage(
    query: UserUsageQuery,
    response: Response,
    project_id: str = Depends(require_project),
    get_client: Callable[[], Client] = Depends(clickhouse_getter),
    memo: UsageMemo = Depends(get_user_usage_memo),
    executor: ThreadPoolExecutor = Depends(get_user_usage_executor),
    stats: UsageReadStats = Depends(get_user_read_stats),
) -> UsageRead:
    """SDK read of user_usage.* for the bearer's project, summed over every agent.
    A user with no activity reads zeros (there is no user registry, and a 404 would
    reveal which users exist). 422 on a bad path or user_id, 503 when ClickHouse
    is unavailable."""
    _check_user_id(query.user_id)
    requested = _parse_or_422(parse_usage_path_list, query.paths)

    read = await _read_windows(
        rollup=USER_ROLLUP,
        project_id=project_id,
        key=query.user_id,
        requested=requested,
        get_client=get_client,
        memo=memo,
        executor=executor,
        stats=stats,
        failure_subject=f"a user in project {project_id}",
    )
    response.headers["Cache-Control"] = USER_USAGE_CACHE_CONTROL
    return read


def _check_user_id(user_id: str) -> None:
    """Never echo the value: it is personal data (README G10). Not a pydantic
    Field constraint: that counts characters, not bytes, and its 422 echoes the
    input."""
    if not user_id:
        raise _unprocessable(_USER_ID_EMPTY)
    try:
        encoded = user_id.encode(_USER_ID_ENCODING)
    except UnicodeEncodeError as exc:  # a lone surrogate from a \ud800 escape
        raise _unprocessable(_USER_ID_NOT_UTF8) from exc
    if len(encoded) > USER_ID_MAX_BYTES:
        raise _unprocessable(_USER_ID_TOO_LONG)


def _parse_or_422[T](
    parse: Callable[[T], dict[str, UsageWindowSpec]], raw: T
) -> dict[str, UsageWindowSpec]:
    try:
        return parse(raw)
    except InvalidUsagePaths as exc:
        raise _unprocessable(str(exc)) from exc


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail
    )


async def _read_windows(
    *,
    rollup: UsageRollup,
    project_id: str,
    key: str,
    requested: Mapping[str, UsageWindowSpec],
    get_client: Callable[[], Client],
    memo: UsageMemo,
    executor: ThreadPoolExecutor,
    stats: UsageReadStats,
    failure_subject: str,
) -> UsageRead:
    specs = tuple(sorted(set(requested.values())))
    queried = False

    def load() -> Awaitable[UsageReadout]:
        nonlocal queried
        queried = True
        # Inside the loader, so a stalled read fails the shared task (and isn't
        # memoized) rather than releasing one caller.
        return asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                executor, _connect_and_read, get_client, rollup, project_id, key, specs
            ),
            timeout=USAGE_READ_TIMEOUT_SECONDS,
        )

    started = time.perf_counter()
    failed = True
    try:
        readout = await memo.get_or_load((project_id, key, specs), load)
        failed = False
    except (ClickHouseError, TimeoutError) as exc:
        # The class only: a ClickHouse message can quote a bound value (README G10).
        _log.warning(
            "usage read failed for %s: %s", failure_subject, type(exc).__name__
        )
        raise clickhouse_unavailable(_USAGE_UNAVAILABLE) from exc
    finally:
        stats.record(
            latency_seconds=time.perf_counter() - started,
            queried=queried,
            failed=failed,
        )
    return UsageRead(
        as_of=readout.as_of,
        values={path: readout.values[spec] for path, spec in requested.items()},
    )


def _connect_and_read(
    get_client: Callable[[], Client],
    rollup: UsageRollup,
    project_id: str,
    key: str,
    specs: tuple[UsageWindowSpec, ...],
) -> UsageReadout:
    """The connect is lazy: it runs after the request checks, inside the read's
    timeout, and a failure to connect is the same 503 as a failed read."""
    return read_usage(get_client(), rollup, project_id, key, specs)
