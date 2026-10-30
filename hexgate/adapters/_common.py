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
from hexgate.runtime.agent_usage import UsageScopeKind

if TYPE_CHECKING:
    from hexgate.security.bans import BanGate
    from hexgate.security.binding import PolicyBinding
    from hexgate.security.enforcer import UsageRefresh

_log = logging.getLogger(__name__)

# Langfuse silently drops a propagated metadata value over 200 chars, so the
# joined role list is truncated to fit (with an ASCII ellipsis — non-ASCII
# values are dropped too). Only bites on an unusually large role list.
_MAX_METADATA_CHARS = 200

# The ban fetch and each usage scope's refresh, each on its own worker.
_MAX_PREFETCH_WORKERS = 3
_USAGE_REFRESH_FAILED = "usage refresh for %s failed"
_USER_USAGE_REFRESH_FAILED = "usage refresh for %s failed (%s)"


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
    binding: PolicyBinding | None,
) -> None:
    """Pre-run work shared by every boundary: the policy refresh, the ban fetch and
    each usage scope's refresh run concurrently, then the ban decision. Admission
    stays with the caller, after this returns, so it reads the refreshed policy.

    The concurrent usage refreshes ask for the pre-refresh policy's paths (02 §5.5
    Rule 2); paths the policy refresh adds are refreshed concurrently once it
    returns."""
    usages = _usage_refreshes_of(binding, context)
    if ban_gate is None and not usages:
        await refresh_policy
    else:
        await _aprefetch_alongside(refresh_policy, ban_gate, context, usages)
    added = _usage_added_by_refresh(binding, context, usages)
    if added:
        await asyncio.gather(*(_arefresh_usage(usage) for usage in added))


def prepare_run(
    refresh_policy: Callable[[], None],
    ban_gate: BanGate | None,
    context: HexgateContext | None,
    *,
    binding: PolicyBinding | None,
) -> None:
    """Sync mirror of :func:`aprepare_run`: the ban and usage fetches run on worker
    threads while the policy refresh runs on the caller's."""
    usages = _usage_refreshes_of(binding, context)
    if ban_gate is None and not usages:
        refresh_policy()
    else:
        _prefetch_alongside(refresh_policy, ban_gate, context, usages)
    _refresh_on_workers(_usage_added_by_refresh(binding, context, usages))


async def _aprefetch_alongside(
    refresh_policy: Awaitable[None],
    ban_gate: BanGate | None,
    context: HexgateContext | None,
    usages: tuple[UsageRefresh, ...],
) -> None:
    fetches: list[Awaitable[Any]] = [refresh_policy]
    fetches.extend(_arefresh_usage(usage) for usage in usages)
    if ban_gate is None:
        await asyncio.gather(*fetches)
        return
    *_, bans = await asyncio.gather(*fetches, asyncio.to_thread(ban_gate.fetch))
    ban_gate.enforce(bans, context)


def _prefetch_alongside(
    refresh_policy: Callable[[], None],
    ban_gate: BanGate | None,
    context: HexgateContext | None,
    usages: tuple[UsageRefresh, ...],
) -> None:
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
        pending_usages = [
            pool.submit(contextvars.copy_context().run, _refresh_usage, usage)
            for usage in usages
        ]
        refresh_policy()
        for pending in pending_usages:
            pending.result()
        bans = pending_bans.result() if pending_bans is not None else None
    finally:
        # The fetches are done on success; on an interrupt, don't block on them.
        pool.shutdown(wait=False, cancel_futures=True)
    if ban_gate is not None:
        ban_gate.enforce(bans, context)


def _refresh_on_workers(usages: tuple[UsageRefresh, ...]) -> None:
    """One worker per refresh, so no scope waits behind another's."""
    if not usages:
        return
    pool = ThreadPoolExecutor(max_workers=len(usages))
    try:
        pending = [
            pool.submit(contextvars.copy_context().run, _refresh_usage, usage)
            for usage in usages
        ]
        for job in pending:
            job.result()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _usage_refreshes_of(
    binding: PolicyBinding | None, context: HexgateContext | None
) -> tuple[UsageRefresh, ...]:
    return binding.enforcer.usage_refreshes(context) if binding is not None else ()


def _usage_added_by_refresh(
    binding: PolicyBinding | None,
    context: HexgateContext | None,
    before: tuple[UsageRefresh, ...],
) -> tuple[UsageRefresh, ...]:
    """The refreshed policy's usage refreshes whose paths the prefetch didn't ask
    for, per scope. Without them, a policy's first usage cap has no snapshot, so its
    first decisions read ``UNAVAILABLE`` and fall back to the local ledger."""
    asked = {usage.scope: usage.paths for usage in before}
    return tuple(
        usage
        for usage in _usage_refreshes_of(binding, context)
        if not usage.paths <= asked.get(usage.scope, frozenset())
    )


def _refresh_usage(usage: UsageRefresh) -> None:
    try:
        usage.run()
    except Exception as exc:  # noqa: BLE001 — a usage refresh must never fail a run
        _log_refresh_failure(usage, exc)


async def _arefresh_usage(usage: UsageRefresh) -> None:
    try:
        await usage.arun()
    except Exception as exc:  # noqa: BLE001 — a usage refresh must never fail a run
        _log_refresh_failure(usage, exc)


def _log_refresh_failure(usage: UsageRefresh, exc: Exception) -> None:
    # A user failure's message may carry the user's ID; log its class only.
    if usage.scope.kind is UsageScopeKind.USER:
        _log.warning(_USER_USAGE_REFRESH_FAILED, usage.scope, type(exc).__name__)
    else:
        _log.warning(_USAGE_REFRESH_FAILED, usage.scope, exc_info=exc)
