"""SDK reads of ``agent_usage.*`` and ``user_usage.*`` windows (bearer; the project
comes from it), and the dashboard's top-users ranking (cookie)."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.clickhouse import server_error_code
from hexgate_api.core.db import get_session
from hexgate_api.deps.clickhouse import clickhouse_getter, clickhouse_unavailable
from hexgate_api.deps.org import require_org_member
from hexgate_api.deps.tokens import require_project
from hexgate_api.features.agents.service import get_agent
from hexgate_api.schemas import USER_ID_MAX_LENGTH
from hexgate_api.features.usage.paths import (
    InvalidUsagePaths,
    UsageMetric,
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
from hexgate_api.features.usage.top_users import (
    TOP_USERS_DEFAULT_LIMIT,
    TOP_USERS_MAX_LIMIT,
    TOP_USERS_WINDOW_SECONDS,
    TopUser,
    TopUsersWindow,
    read_top_users,
)

_log = logging.getLogger(__name__)

router = APIRouter()

CACHE_CONTROL: Final = f"private, max-age={int(USAGE_MEMO_TTL_SECONDS)}"
# The body is keyed by personal data: no intermediary may keep it.
USER_USAGE_CACHE_CONTROL: Final = "private, no-store"
_USER_ID_ENCODING: Final = "utf-8"
_AGENT_NOT_FOUND: Final = "agent not found"
_USAGE_UNAVAILABLE: Final = "usage temporarily unavailable"
_USER_ID_EMPTY: Final = "user_id is empty"
_USER_ID_TOO_LONG: Final = f"user_id is longer than {USER_ID_MAX_LENGTH} characters"
_USER_ID_NOT_UTF8: Final = "user_id is not valid UTF-8"


@dataclass(frozen=True, slots=True)
class UsageScope:
    """One rollup with its own memo, pool and stats, so a burst on one scope
    cannot queue or evict the other's reads."""

    rollup: UsageRollup
    memo: UsageMemo
    executor: ThreadPoolExecutor
    stats: UsageReadStats


def get_agent_scope(
    memo: UsageMemo = Depends(get_usage_memo),
    executor: ThreadPoolExecutor = Depends(get_usage_executor),
    stats: UsageReadStats = Depends(get_agent_read_stats),
) -> UsageScope:
    return UsageScope(AGENT_ROLLUP, memo, executor, stats)


def get_user_scope(
    memo: UsageMemo = Depends(get_user_usage_memo),
    executor: ThreadPoolExecutor = Depends(get_user_usage_executor),
    stats: UsageReadStats = Depends(get_user_read_stats),
) -> UsageScope:
    return UsageScope(USER_ROLLUP, memo, executor, stats)


class UsageRead(BaseModel):
    """``values`` is keyed by the paths as requested."""

    as_of: datetime
    values: dict[str, int]


class TopUsersRead(BaseModel):
    """Ranked by ``value`` descending; users at 0 are left out."""

    window: TopUsersWindow
    metric: UsageMetric
    users: list[TopUser]


class UserUsageQuery(BaseModel):
    """``POST /v1/usage/users:read`` body. user_id goes in the body, never the
    URL. An object, so a batch or a verified flag can be added later."""

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
    scope: UsageScope = Depends(get_agent_scope),
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
        scope,
        project_id=project_id,
        key=name,
        requested=requested,
        get_client=get_client,
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
    scope: UsageScope = Depends(get_user_scope),
) -> UsageRead:
    """SDK read of user_usage.* for the bearer's project, summed over every agent.
    A user with no activity reads zeros (there is no user registry, and a 404 would
    reveal which users exist). 422 on a bad path or user_id, 503 when ClickHouse
    is unavailable."""
    _check_user_id(query.user_id)
    requested = _parse_or_422(parse_usage_path_list, query.paths)

    read = await _read_windows(
        scope,
        project_id=project_id,
        key=query.user_id,
        requested=requested,
        get_client=get_client,
        failure_subject=f"a user in project {project_id}",
    )
    response.headers["Cache-Control"] = USER_USAGE_CACHE_CONTROL
    return read


@router.get(
    "/projects/{project_id}/usage/top-users",
    response_model=TopUsersRead,
    dependencies=[Depends(require_org_member)],
    tags=["usage"],
)
async def api_top_users(
    project_id: str,
    response: Response,
    window: TopUsersWindow = "24h",
    metric: UsageMetric = UsageMetric.TOTAL_TOKENS,
    limit: int = Query(TOP_USERS_DEFAULT_LIMIT, ge=1, le=TOP_USERS_MAX_LIMIT),
    get_client: Callable[[], Client] = Depends(clickhouse_getter),
) -> TopUsersRead:
    """Dashboard ranking of the project's end users across every agent. Runs on
    the default thread pool, never the SDK user-read pool."""
    try:
        users = await asyncio.to_thread(
            _connect_and_rank,
            get_client,
            project_id,
            TOP_USERS_WINDOW_SECONDS[window],
            metric,
            limit,
        )
    except ClickHouseError as exc:
        raise _usage_unavailable(exc, f"top users of project {project_id}") from exc
    response.headers["Cache-Control"] = USER_USAGE_CACHE_CONTROL
    return TopUsersRead(window=window, metric=metric, users=users)


def _check_user_id(user_id: str) -> None:
    """Never echo the value: it is personal data. Not a pydantic
    Field constraint, because its 422 echoes the input. The length cap is ingest's
    (AuditEnvelope.user_id), so every id the rollup counts can be read back."""
    if not user_id:
        raise _unprocessable(_USER_ID_EMPTY)
    if len(user_id) > USER_ID_MAX_LENGTH:
        raise _unprocessable(_USER_ID_TOO_LONG)
    try:
        user_id.encode(_USER_ID_ENCODING)
    except UnicodeEncodeError as exc:  # a lone surrogate from a \ud800 escape
        raise _unprocessable(_USER_ID_NOT_UTF8) from exc


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


def _usage_unavailable(exc: Exception, subject: str) -> HTTPException:
    """Logs the class and the numeric server code only: a ClickHouse message can
    quote a bound value."""
    _log.warning(
        "usage read failed for %s: %s (clickhouse code %s)",
        subject,
        type(exc).__name__,
        server_error_code(exc),
    )
    return clickhouse_unavailable(_USAGE_UNAVAILABLE)


async def _read_windows(
    scope: UsageScope,
    *,
    project_id: str,
    key: str,
    requested: Mapping[str, UsageWindowSpec],
    get_client: Callable[[], Client],
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
                scope.executor,
                _connect_and_read,
                get_client,
                scope.rollup,
                project_id,
                key,
                specs,
            ),
            timeout=USAGE_READ_TIMEOUT_SECONDS,
        )

    started = time.perf_counter()
    failed = True
    try:
        readout = await scope.memo.get_or_load((project_id, key, specs), load)
        failed = False
    except (ClickHouseError, TimeoutError) as exc:
        raise _usage_unavailable(exc, failure_subject) from exc
    finally:
        scope.stats.record(
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


def _connect_and_rank(
    get_client: Callable[[], Client],
    project_id: str,
    window_seconds: int,
    metric: UsageMetric,
    limit: int,
) -> list[TopUser]:
    """Lazy connect, as in _connect_and_read: a connect failure is the route's
    own 503 and log line, not require_clickhouse's audit one."""
    return read_top_users(get_client(), project_id, window_seconds, metric, limit)
