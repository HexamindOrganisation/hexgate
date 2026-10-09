"""Tests for hexgate.adapters._common: Langfuse propagation and the pre-run seam."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from types import SimpleNamespace

import pytest

from hexgate.adapters import _common
from hexgate.adapters._common import (
    _MAX_METADATA_CHARS,
    aprepare_run,
    langfuse_propagate_kwargs,
    prepare_run,
    usage_refresh_of,
)
from hexgate.runtime import HexgateContext
from hexgate.security.bans import EMPTY_BAN_SET, BanGate, BanSet, ban_set_from_payload
from hexgate.security.enforcer import UsageRefresh
from hexgate.security.errors import AgentBannedError

# Long enough never to flake, short enough that a sequential regression fails
# fast: run one after the other, the first party waits alone and breaks it.
_BARRIER_TIMEOUT_S = 2.0
_AGENT = "bot"


def test_propagate_kwargs_joins_roles_as_string() -> None:
    """Multiple roles stamp as a comma-joined string (Langfuse drops non-str)."""
    ctx = HexgateContext(user_id="u", session_id="s", user_roles=["billing", "admin"])
    kwargs = langfuse_propagate_kwargs(ctx, "tag")
    assert kwargs["metadata"] == {"user_roles": "billing, admin"}
    assert kwargs["user_id"] == "u"
    assert kwargs["tags"] == ["tag"]


def test_propagate_kwargs_truncates_over_the_langfuse_cap() -> None:
    """A large role list is truncated to <=200 chars so Langfuse doesn't drop it."""
    ctx = HexgateContext(user_id="u", user_roles=[f"role_{i:03d}" for i in range(100)])
    roles = langfuse_propagate_kwargs(ctx, "tag")["metadata"]["user_roles"]
    assert len(roles) <= _MAX_METADATA_CHARS
    assert roles.endswith("...")


class _BanSource:
    """Returns ``bans``; with a barrier, only once the policy refresh is
    waiting on it too."""

    def __init__(
        self, bans: BanSet = EMPTY_BAN_SET, barrier: threading.Barrier | None = None
    ) -> None:
        self._bans = bans
        self._barrier = barrier

    def fetch(self) -> BanSet:
        if self._barrier is not None:
            self._barrier.wait()
        return self._bans


class _Refresh:
    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self._barrier = barrier
        self.calls = 0

    def __call__(self) -> None:
        if self._barrier is not None:
            self._barrier.wait()
        self.calls += 1

    async def run_async(self) -> None:
        await asyncio.to_thread(self)


def _barrier() -> threading.Barrier:
    return threading.Barrier(2, timeout=_BARRIER_TIMEOUT_S)


def _context() -> HexgateContext:
    return HexgateContext(user_id="u", session_id="s")


def _banning_gate() -> BanGate:
    bans = ban_set_from_payload(
        [
            {
                "ban_id": "b1",
                "ban_type": "agent",
                "target_agent_name": _AGENT,
                "target_user_id": None,
                "reason": "agent disabled",
            }
        ]
    )
    return BanGate(_AGENT, _BanSource(bans))


async def test_aprepare_run_fetches_concurrently() -> None:
    barrier = _barrier()
    refresh = _Refresh(barrier)

    await aprepare_run(
        refresh.run_async(), BanGate(_AGENT, _BanSource(barrier=barrier)), _context()
    )

    assert refresh.calls == 1


def test_prepare_run_fetches_concurrently() -> None:
    barrier = _barrier()
    refresh = _Refresh(barrier)

    prepare_run(refresh, BanGate(_AGENT, _BanSource(barrier=barrier)), _context())

    assert refresh.calls == 1


_CALLER_MARK: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_CALLER_MARK", default=None
)
_MARK = "caller"


class _ContextReadingBanSource:
    def __init__(self) -> None:
        self.seen: str | None = None

    def fetch(self) -> BanSet:
        self.seen = _CALLER_MARK.get()
        return EMPTY_BAN_SET


def test_prepare_run_fetches_in_the_callers_context() -> None:
    """Like asyncio.to_thread on the async path, so host tracing and log
    filters see the same context on sync and async boundaries."""
    source = _ContextReadingBanSource()
    token = _CALLER_MARK.set(_MARK)
    try:
        prepare_run(_Refresh(), BanGate(_AGENT, source), _context())
    finally:
        _CALLER_MARK.reset(token)

    assert source.seen == _MARK


class _BlockedBanSource:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.finished = threading.Event()

    def fetch(self) -> BanSet:
        self.release.wait(_BARRIER_TIMEOUT_S)
        self.finished.set()
        return EMPTY_BAN_SET


def test_prepare_run_interrupt_does_not_wait_for_the_ban_fetch() -> None:
    """Ctrl-C during the refresh surfaces at once, not after the fetch."""
    source = _BlockedBanSource()

    def _interrupted() -> None:
        raise KeyboardInterrupt

    try:
        with pytest.raises(KeyboardInterrupt):
            prepare_run(_interrupted, BanGate(_AGENT, source), _context())
        assert not source.finished.is_set()
    finally:
        source.release.set()


async def test_aprepare_run_refuses_after_refresh_completes() -> None:
    """Deciding before the refresh lands would let admission read stale policy."""
    refresh = _Refresh()

    with pytest.raises(AgentBannedError):
        await aprepare_run(refresh.run_async(), _banning_gate(), _context())

    assert refresh.calls == 1


def test_prepare_run_refuses_after_refresh_completes() -> None:
    refresh = _Refresh()

    with pytest.raises(AgentBannedError):
        prepare_run(refresh, _banning_gate(), _context())

    assert refresh.calls == 1


async def test_aprepare_run_without_gate_only_refreshes() -> None:
    refresh = _Refresh()

    await aprepare_run(refresh.run_async(), None, _context())

    assert refresh.calls == 1


def test_prepare_run_without_gate_only_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_executor(*_: object, **__: object) -> None:
        raise AssertionError("no ban gate, so no worker thread")

    monkeypatch.setattr(_common, "ThreadPoolExecutor", _no_executor)
    refresh = _Refresh()

    prepare_run(refresh, None, _context())

    assert refresh.calls == 1


class _UsageSource:
    """A usage source whose refresh can wait on a barrier, or raise."""

    def __init__(
        self,
        barrier: threading.Barrier | None = None,
        error: Exception | None = None,
    ) -> None:
        self._barrier = barrier
        self._error = error
        self.calls: list[tuple[str, frozenset[str]]] = []

    def refresh(self, agent: str, paths: frozenset[str]) -> None:
        if self._barrier is not None:
            self._barrier.wait()
        if self._error is not None:
            raise self._error
        self.calls.append((agent, paths))

    async def refresh_async(self, agent: str, paths: frozenset[str]) -> None:
        await asyncio.to_thread(self.refresh, agent, paths)


_USAGE_PATHS = frozenset({"invocations_1h"})


def _usage(source: _UsageSource) -> UsageRefresh:
    return UsageRefresh(source, _AGENT, _USAGE_PATHS)  # type: ignore[arg-type]


def _parties(count: int) -> threading.Barrier:
    return threading.Barrier(count, timeout=_BARRIER_TIMEOUT_S)


async def test_aprepare_run_without_gate_or_usage_awaits_the_refresh_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The latency invariant: a usage-free agent's boundary is unchanged. A gather
    around a lone awaitable would wrap it in a task and copy the context."""

    def _no_gather(*_: object, **__: object) -> None:
        raise AssertionError("nothing to overlap, so no gather")

    monkeypatch.setattr(_common.asyncio, "gather", _no_gather)
    refresh = _Refresh()

    await aprepare_run(refresh.run_async(), None, _context(), usage=None)

    assert refresh.calls == 1


@pytest.mark.parametrize("with_gate", [False, True])
async def test_aprepare_run_refreshes_usage_concurrently(with_gate: bool) -> None:
    barrier = _parties(3 if with_gate else 2)
    refresh = _Refresh(barrier)
    source = _UsageSource(barrier)
    gate = BanGate(_AGENT, _BanSource(barrier=barrier)) if with_gate else None

    await aprepare_run(refresh.run_async(), gate, _context(), usage=_usage(source))

    assert refresh.calls == 1
    assert source.calls == [(_AGENT, _USAGE_PATHS)]


@pytest.mark.parametrize("with_gate", [False, True])
def test_prepare_run_refreshes_usage_concurrently(with_gate: bool) -> None:
    barrier = _parties(3 if with_gate else 2)
    refresh = _Refresh(barrier)
    source = _UsageSource(barrier)
    gate = BanGate(_AGENT, _BanSource(barrier=barrier)) if with_gate else None

    prepare_run(refresh, gate, _context(), usage=_usage(source))

    assert refresh.calls == 1
    assert source.calls == [(_AGENT, _USAGE_PATHS)]


def test_prepare_run_gives_each_worker_its_own_context() -> None:
    """Both workers run at once: one shared Context would make the second
    ``Context.run`` raise RuntimeError."""
    barrier = _barrier()
    source = _UsageSource(barrier)

    prepare_run(
        _Refresh(),
        BanGate(_AGENT, _BanSource(barrier=barrier)),
        _context(),
        usage=_usage(source),
    )

    assert source.calls == [(_AGENT, _USAGE_PATHS)]


async def test_a_failing_async_usage_refresh_does_not_fail_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refresh = _Refresh()
    source = _UsageSource(error=RuntimeError("usage bug"))

    with caplog.at_level(logging.WARNING, logger=_common.__name__):
        await aprepare_run(refresh.run_async(), None, _context(), usage=_usage(source))

    assert refresh.calls == 1
    assert len(caplog.records) == 1


def test_a_failing_usage_refresh_does_not_fail_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refresh = _Refresh()
    source = _UsageSource(error=RuntimeError("usage bug"))

    with caplog.at_level(logging.WARNING, logger=_common.__name__):
        prepare_run(refresh, None, _context(), usage=_usage(source))

    assert refresh.calls == 1
    assert len(caplog.records) == 1


async def test_a_failing_async_usage_refresh_still_lets_the_ban_decide() -> None:
    source = _UsageSource(error=RuntimeError("usage bug"))

    with pytest.raises(AgentBannedError):
        await aprepare_run(
            _Refresh().run_async(), _banning_gate(), _context(), usage=_usage(source)
        )


def test_a_failing_usage_refresh_still_lets_the_ban_decide() -> None:
    source = _UsageSource(error=RuntimeError("usage bug"))

    with pytest.raises(AgentBannedError):
        prepare_run(_Refresh(), _banning_gate(), _context(), usage=_usage(source))


async def test_aprepare_run_refuses_a_ban_after_every_fetch_completes() -> None:
    refresh = _Refresh()
    source = _UsageSource()

    with pytest.raises(AgentBannedError):
        await aprepare_run(
            refresh.run_async(), _banning_gate(), _context(), usage=_usage(source)
        )

    assert refresh.calls == 1
    assert len(source.calls) == 1


def test_prepare_run_refuses_a_ban_after_every_fetch_completes() -> None:
    refresh = _Refresh()
    source = _UsageSource()

    with pytest.raises(AgentBannedError):
        prepare_run(refresh, _banning_gate(), _context(), usage=_usage(source))

    assert refresh.calls == 1
    assert len(source.calls) == 1


def test_no_binding_means_no_usage_refresh() -> None:
    assert usage_refresh_of(None) is None


def test_a_binding_supplies_its_enforcers_usage_refresh() -> None:
    usage = _usage(_UsageSource())
    binding = SimpleNamespace(enforcer=SimpleNamespace(usage_refresh=lambda: usage))

    assert usage_refresh_of(binding) is usage  # type: ignore[arg-type]
