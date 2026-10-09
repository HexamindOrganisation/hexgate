"""Internal helpers shared across all four framework adapters.

Not part of the public API — each adapter's own module is the supported
import surface.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterator

from langfuse import propagate_attributes

from hexgate.runtime import HexgateContext, run_scope

if TYPE_CHECKING:
    from hexgate.security.bans import BanGate
    from hexgate.security.binding import PolicyBinding
    from hexgate.security.enforcer import UsageRefresh

_log = logging.getLogger(__name__)

# Langfuse silently drops a propagated metadata value over 200 chars, so the
# joined role list is truncated to fit (with an ASCII ellipsis — non-ASCII
# values are dropped too). Only bites on an unusually large role list.
_MAX_METADATA_CHARS = 200

# The ban fetch and the usage refresh, each on its own worker.
_MAX_PREFETCH_WORKERS = 2
_USAGE_REFRESH_FAILED = "usage refresh for agent %r failed"


def langfuse_propagate_kwargs(context: HexgateContext, tag: str) -> dict[str, Any]:
    """Build the ``propagate_attributes(**kwargs)`` mapping for a Langfuse
    span tagged ``tag``, carrying the active context's identity."""
    # Langfuse drops non-string metadata values, so stamp the role list as a
    # comma-joined string (not the lossy single role), truncated to the cap.
    roles = ", ".join(context.user_roles)
    if len(roles) > _MAX_METADATA_CHARS:
        roles = roles[: _MAX_METADATA_CHARS - 3] + "..."
    return {
        "tags": [tag],
        "user_id": context.user_id,
        "session_id": context.session_id,
        "metadata": {"user_roles": roles},
    }


@asynccontextmanager
async def abind(
    context: HexgateContext,
    agent_name: str,
    tag: str,
    *,
    api_key: str | None = None,
) -> AsyncIterator[None]:
    """Async run boundary shared by every adapter proxy: identity scope, run facts,
    then Langfuse propagation, so the facts are live wherever a tool call executes.

    Takes the span ``tag`` rather than the built kwargs — it embeds the caller's
    method name, and resolving it here keeps the attributes read inside the scopes.
    """
    async with context:
        with run_scope(agent_name, api_key=api_key):
            with propagate_attributes(**langfuse_propagate_kwargs(context, tag)):
                yield


@contextmanager
def bind(
    context: HexgateContext,
    agent_name: str,
    tag: str,
    *,
    api_key: str | None = None,
) -> Iterator[None]:
    """Sync mirror of :func:`abind`."""
    with context.sync_scope():
        with run_scope(agent_name, api_key=api_key):
            with propagate_attributes(**langfuse_propagate_kwargs(context, tag)):
                yield


async def aprepare_run(
    refresh_policy: Awaitable[None],
    ban_gate: BanGate | None,
    context: HexgateContext | None,
    *,
    usage: UsageRefresh | None = None,
) -> None:
    """Pre-run work shared by every boundary: the policy refresh, the ban fetch and
    the usage refresh run concurrently, then the ban decision. Admission stays with
    the caller, after this returns, so it reads the refreshed policy."""
    if ban_gate is None and usage is None:
        await refresh_policy
        return
    fetches: list[Awaitable[Any]] = [refresh_policy]
    if usage is not None:
        fetches.append(_arefresh_usage(usage))
    if ban_gate is None:
        await asyncio.gather(*fetches)
        return
    *_, bans = await asyncio.gather(*fetches, asyncio.to_thread(ban_gate.fetch))
    ban_gate.enforce(bans, context)


def prepare_run(
    refresh_policy: Callable[[], None],
    ban_gate: BanGate | None,
    context: HexgateContext | None,
    *,
    usage: UsageRefresh | None = None,
) -> None:
    """Sync mirror of :func:`aprepare_run`: the ban and usage fetches run on worker
    threads while the policy refresh runs on the caller's."""
    if ban_gate is None and usage is None:
        refresh_policy()
        return
    # Per call, not module-level: a shared pool used before a fork never runs
    # work in the child.
    pool = ThreadPoolExecutor(max_workers=_MAX_PREFETCH_WORKERS)
    try:
        # Copied like asyncio.to_thread does, so the fetches see the caller's
        # context (host tracing, log filters) on sync and async paths alike. One
        # copy per job: a Context can't be entered by two threads at once.
        pending_bans = (
            pool.submit(contextvars.copy_context().run, ban_gate.fetch)
            if ban_gate is not None
            else None
        )
        pending_usage = (
            pool.submit(contextvars.copy_context().run, _refresh_usage, usage)
            if usage is not None
            else None
        )
        refresh_policy()
        if pending_usage is not None:
            pending_usage.result()
        bans = pending_bans.result() if pending_bans is not None else None
    finally:
        # The fetches are done on success; on an interrupt, don't block on them.
        pool.shutdown(wait=False, cancel_futures=True)
    if ban_gate is not None:
        ban_gate.enforce(bans, context)


def usage_refresh_of(binding: PolicyBinding | None) -> UsageRefresh | None:
    return binding.enforcer.usage_refresh() if binding is not None else None


def _refresh_usage(usage: UsageRefresh) -> None:
    try:
        usage.run()
    except Exception:  # noqa: BLE001 — a usage refresh must never fail a run
        _log.warning(_USAGE_REFRESH_FAILED, usage.agent_name, exc_info=True)


async def _arefresh_usage(usage: UsageRefresh) -> None:
    try:
        await usage.arun()
    except Exception:  # noqa: BLE001 — a usage refresh must never fail a run
        _log.warning(_USAGE_REFRESH_FAILED, usage.agent_name, exc_info=True)
