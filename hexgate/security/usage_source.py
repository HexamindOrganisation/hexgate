"""The platform term of ``agent_usage.*``.

Keeps one snapshot per agent of ``GET /v1/agents/{name}/usage``, refreshed
stale-while-revalidate, and combines it with the process-local ledger into the
namespace a decision reads. Shared per ``(api_key, base_url)`` like
:class:`~hexgate.security.bans.PlatformBanSource`, and fail-soft: a platform blip
degrades to ledger-only values by default, never to an error. Inert until the run
boundary calls it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Protocol

from hexgate.cloud.client import HexgateError
from hexgate.config.env import resolve_api_key
from hexgate.runtime.agent_usage import (
    Clock,
    UsageLedger,
    _monotonic,
    combined_namespace,
    ledger_namespace,
)
from hexgate.security.source import _LOCAL_POLICY_ENV_VAR
from hexgate.tracing._senders import _local_mode_active

if TYPE_CHECKING:
    from hexgate.cloud.client import HexgateClient

logger = logging.getLogger("hexgate.security.usage_source")

# Above the 5-15 s span pipeline lag, so own events not yet ingested when the
# snapshot is fetched are still counted locally (gate G7).
USAGE_INGEST_MARGIN_SECONDS: Final[float] = 30.0

_MIN_REFRESH_ENV: Final = "HEXGATE_USAGE_REFRESH_SECONDS"
_SYNC_AFTER_ENV: Final = "HEXGATE_USAGE_SYNC_AFTER_SECONDS"
_MAX_STALENESS_ENV: Final = "HEXGATE_USAGE_MAX_STALENESS_SECONDS"
DEFAULT_MIN_REFRESH_SECONDS: Final[float] = 5.0
DEFAULT_SYNC_AFTER_SECONDS: Final[float] = 60.0
DEFAULT_MAX_STALENESS_SECONDS: Final[float] = 300.0

_VALUES_KEY: Final = "values"
_NOT_FOUND: Final = 404
_EXECUTOR_THREAD_PREFIX: Final = "hexgate-usage"


class OnUnavailable(StrEnum):
    """What a decision reads when the platform term is missing."""

    ALLOW = "allow"  # ledger-only values: zeros plus this process (G3)
    DENY = "deny"  # the value is absent, so its constraints fail closed


class UsageState(StrEnum):
    """How a namespace was built. Precedence when several apply:
    ``UNAVAILABLE`` > ``PARTIAL`` > ``STALE`` > ``FRESH``."""

    FRESH = "fresh"  # snapshot younger than sync_after, every path in it
    STALE = "stale"  # last-good older than sync_after, under max_staleness
    PARTIAL = "partial"  # usable snapshot, some paths missing from it (F5)
    UNAVAILABLE = "unavailable"  # no snapshot, or older than max_staleness
    LOCAL = "local"  # no platform: local mode, local policy or no key


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    values: Mapping[str, int]  # a requested path may be absent
    fetched_at: float  # monotonic, at fetch start


@dataclass(frozen=True, slots=True)
class UsageReading:
    namespace: dict[str, int] | None  # None only under DENY with nothing to read
    state: UsageState


@dataclass(frozen=True, slots=True)
class UsageRefreshSettings:
    min_refresh_seconds: float = DEFAULT_MIN_REFRESH_SECONDS
    sync_after_seconds: float = DEFAULT_SYNC_AFTER_SECONDS
    max_staleness_seconds: float = DEFAULT_MAX_STALENESS_SECONDS

    def __post_init__(self) -> None:
        if not (
            0
            < self.min_refresh_seconds
            <= self.sync_after_seconds
            <= self.max_staleness_seconds
        ):
            raise ValueError(
                "usage refresh thresholds must satisfy 0 < min_refresh <= "
                f"sync_after <= max_staleness, got {self.min_refresh_seconds}, "
                f"{self.sync_after_seconds}, {self.max_staleness_seconds}"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> UsageRefreshSettings:
        """Read the thresholds from ``HEXGATE_USAGE_*``. A bad value logs and falls
        back to the defaults: it runs at the run boundary and must not stop agents."""
        try:
            return cls(
                min_refresh_seconds=float(
                    environ.get(_MIN_REFRESH_ENV, DEFAULT_MIN_REFRESH_SECONDS)
                ),
                sync_after_seconds=float(
                    environ.get(_SYNC_AFTER_ENV, DEFAULT_SYNC_AFTER_SECONDS)
                ),
                max_staleness_seconds=float(
                    environ.get(_MAX_STALENESS_ENV, DEFAULT_MAX_STALENESS_SECONDS)
                ),
            )
        except ValueError as exc:
            logger.warning("ignoring usage refresh settings, using defaults: %s", exc)
            return cls()


class UsageContentError(RuntimeError):
    """A 200 whose body is not ``{"values": {path: int}}`` — contract drift."""


def usage_values_from_payload(payload: Any, paths: frozenset[str]) -> dict[str, int]:
    """The requested values in ``payload``. A requested path absent from the body
    is absent from the result (the per-path case); unrequested keys are dropped."""
    values = payload.get(_VALUES_KEY) if isinstance(payload, dict) else None
    if not isinstance(values, dict):
        raise UsageContentError(
            f"usage body has no {_VALUES_KEY!r} object: {payload!r}"
        )
    parsed: dict[str, int] = {}
    for path in paths & values.keys():
        value = values[path]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise UsageContentError(f"usage value for {path!r} is {value!r}")
        parsed[path] = value
    return parsed


class UsageFetcher(Protocol):
    def get_agent_usage(self, name: str, paths: Sequence[str]) -> Mapping[str, Any]: ...


class _Tier(Enum):
    NONE = "none"
    BACKGROUND = "background"
    SYNC = "sync"


@dataclass(frozen=True, slots=True)
class _Attempt:
    at: float
    paths: frozenset[str]


class PlatformUsageSource:
    """Per-agent usage snapshots: refreshed at the run boundary, read per decision.

    ``refresh`` may block on one bounded fetch when the snapshot is cold or older
    than ``sync_after``; otherwise it schedules at most one background fetch per
    agent. ``read`` never touches the network. Fetches keep last-good on failure,
    and a failed one is not retried within ``min_refresh`` unless the path set grew.
    """

    def __init__(
        self,
        fetcher: UsageFetcher,
        executor: Executor,
        settings: UsageRefreshSettings,
        clock: Clock,
    ) -> None:
        self._fetcher = fetcher
        self._executor = executor
        self._settings = settings
        self._clock = clock
        # Held only for dict reads and writes, never across HTTP.
        self._state_lock = threading.Lock()
        self._snapshots: dict[str, UsageSnapshot] = {}
        # Read when a fetch runs, not when it was scheduled, so a queued fetch
        # picks up a policy's new paths.
        self._wanted: dict[str, frozenset[str]] = {}
        self._last_attempt: dict[str, _Attempt] = {}
        self._in_flight: set[str] = set()
        self._unregistered_logged: set[str] = set()
        # Per agent, so a sync fetch for one agent never waits on another's.
        self._fetch_locks: dict[str, threading.Lock] = {}

    def refresh(self, agent: str, paths: frozenset[str]) -> None:
        tier = self._tier(agent, paths)
        if tier is _Tier.SYNC:
            self._fetch_now(agent)
        elif tier is _Tier.BACKGROUND:
            self._schedule(agent)

    async def refresh_async(self, agent: str, paths: frozenset[str]) -> None:
        """As :meth:`refresh`; only the synchronous fetch leaves the loop."""
        tier = self._tier(agent, paths)
        if tier is _Tier.SYNC:
            await asyncio.to_thread(self._fetch_now, agent)
        elif tier is _Tier.BACKGROUND:
            self._schedule(agent)

    def read(
        self,
        agent: str,
        paths: frozenset[str],
        ledger: UsageLedger,
        *,
        on_unavailable: OnUnavailable = OnUnavailable.ALLOW,
        current_run_age: float | None = None,
    ) -> UsageReading:
        """The ``agent_usage`` namespace for ``paths``. Anything short of ``FRESH``
        schedules a background refresh, so a long run heals its own snapshot."""
        snapshot = self._remember(agent, paths)
        reading = self._reading(
            snapshot, paths, ledger, on_unavailable, current_run_age
        )
        if reading.state is not UsageState.FRESH:
            self._schedule(agent)
        return reading

    def _reading(
        self,
        snapshot: UsageSnapshot | None,
        paths: frozenset[str],
        ledger: UsageLedger,
        on_unavailable: OnUnavailable,
        current_run_age: float | None,
    ) -> UsageReading:
        if snapshot is None:
            return _unavailable_reading(ledger, paths, on_unavailable, current_run_age)
        age = self._clock() - snapshot.fetched_at
        if age >= self._settings.max_staleness_seconds:
            return _unavailable_reading(ledger, paths, on_unavailable, current_run_age)
        return self._combine(
            snapshot, age, paths, ledger, on_unavailable, current_run_age
        )

    def _combine(
        self,
        snapshot: UsageSnapshot,
        age: float,
        paths: frozenset[str],
        ledger: UsageLedger,
        on_unavailable: OnUnavailable,
        current_run_age: float | None,
    ) -> UsageReading:
        present = {
            path: snapshot.values[path] for path in paths & snapshot.values.keys()
        }
        missing = paths - present.keys()
        namespace = combined_namespace(
            ledger,
            present,
            since=snapshot.fetched_at - USAGE_INGEST_MARGIN_SECONDS,
            since_age=age + USAGE_INGEST_MARGIN_SECONDS,
            current_run_age=current_run_age,
        )
        # Under DENY a missing path stays absent, so only its constraints fail
        # closed; the rest of the policy keeps working (F5).
        if missing and on_unavailable is OnUnavailable.ALLOW:
            namespace |= ledger_namespace(
                ledger, missing, current_run_age=current_run_age
            )
        if missing:
            state = UsageState.PARTIAL
        elif age >= self._settings.sync_after_seconds:
            state = UsageState.STALE
        else:
            state = UsageState.FRESH
        return UsageReading(namespace, state)

    def _remember(self, agent: str, paths: frozenset[str]) -> UsageSnapshot | None:
        with self._state_lock:
            if paths:
                self._wanted[agent] = paths
            return self._snapshots.get(agent)

    def _tier(self, agent: str, paths: frozenset[str]) -> _Tier:
        if not paths:
            return _Tier.NONE
        snapshot = self._remember(agent, paths)
        if snapshot is None:
            return _Tier.SYNC
        age = self._clock() - snapshot.fetched_at
        if age >= self._settings.sync_after_seconds:
            return _Tier.SYNC
        # A new path never blocks the run: it reads ledger-only until this lands (F5).
        if (
            not paths <= snapshot.values.keys()
            or age >= self._settings.min_refresh_seconds
        ):
            return _Tier.BACKGROUND
        return _Tier.NONE

    def _fetch_now(self, agent: str) -> None:
        with self._fetch_lock(agent):
            if self._throttled(agent) or self._covered(agent):
                return
            self._fetch(agent)

    def _schedule(self, agent: str) -> None:
        with self._state_lock:
            if agent in self._in_flight or self._throttled_locked(agent):
                return
            self._in_flight.add(agent)
        try:
            future = self._executor.submit(self._fetch_in_background, agent)
        except RuntimeError as exc:  # executor shut down at interpreter exit
            logger.debug("usage refresh for %r not scheduled: %s", agent, exc)
            self._finish(agent)
            return
        future.add_done_callback(lambda _done: self._finish(agent))

    def _fetch_in_background(self, agent: str) -> None:
        with self._fetch_lock(agent):
            if not self._throttled(agent):
                self._fetch(agent)

    def _finish(self, agent: str) -> None:
        with self._state_lock:
            self._in_flight.discard(agent)

    def _fetch(self, agent: str) -> None:
        """One fetch under ``agent``'s fetch lock. Never raises: last-good stays."""
        with self._state_lock:
            wanted = self._wanted.get(agent, frozenset())
            started = self._clock()
            self._last_attempt[agent] = _Attempt(started, wanted)
        if not wanted:
            return
        try:
            payload = self._fetcher.get_agent_usage(agent, sorted(wanted))
            values = usage_values_from_payload(payload, wanted)
        except UsageContentError as exc:
            logger.error("usage body for %r rejected; using last-good: %s", agent, exc)
            return
        except HexgateError as exc:
            if exc.status == _NOT_FOUND:
                self._log_unregistered(agent)
            else:
                logger.warning(
                    "usage refresh for %r failed; using last-good: %s", agent, exc
                )
            return
        except Exception as exc:  # noqa: BLE001 — fail-soft: keep last-good
            logger.warning(
                "usage refresh for %r failed; using last-good: %s", agent, exc
            )
            return
        with self._state_lock:
            self._snapshots[agent] = UsageSnapshot(MappingProxyType(values), started)

    def _log_unregistered(self, agent: str) -> None:
        # Otherwise it reads as an outage forever, so say what it is, once.
        with self._state_lock:
            if agent in self._unregistered_logged:
                return
            self._unregistered_logged.add(agent)
        logger.warning(
            "agent %r is not registered in this API key's project; "
            "agent_usage.* reads local usage only",
            agent,
        )

    def _throttled(self, agent: str) -> bool:
        with self._state_lock:
            return self._throttled_locked(agent)

    def _throttled_locked(self, agent: str) -> bool:
        last = self._last_attempt.get(agent)
        return (
            last is not None
            and self._clock() - last.at < self._settings.min_refresh_seconds
            and self._wanted.get(agent, frozenset()) <= last.paths
        )

    def _covered(self, agent: str) -> bool:
        """Another fetch finished while this one waited for the lock."""
        with self._state_lock:
            snapshot = self._snapshots.get(agent)
            wanted = self._wanted.get(agent, frozenset())
        return (
            snapshot is not None
            and self._clock() - snapshot.fetched_at < self._settings.sync_after_seconds
            and wanted <= snapshot.values.keys()
        )

    def _fetch_lock(self, agent: str) -> threading.Lock:
        with self._state_lock:
            return self._fetch_locks.setdefault(agent, threading.Lock())


def _unavailable_reading(
    ledger: UsageLedger,
    paths: frozenset[str],
    on_unavailable: OnUnavailable,
    current_run_age: float | None,
) -> UsageReading:
    # Fail open by default (G3): ledger-only values, never an absent namespace.
    if on_unavailable is OnUnavailable.DENY:
        return UsageReading(None, UsageState.UNAVAILABLE)
    return UsageReading(
        ledger_namespace(ledger, paths, current_run_age=current_run_age),
        UsageState.UNAVAILABLE,
    )


def local_reading(
    ledger: UsageLedger,
    paths: frozenset[str],
    *,
    current_run_age: float | None = None,
) -> UsageReading:
    """The reading when there is no platform to ask."""
    return UsageReading(
        ledger_namespace(ledger, paths, current_run_age=current_run_age),
        UsageState.LOCAL,
    )


# One source per (api-key, base-url), as for bans: agents on one key and platform
# share snapshots, and staging and prod never share a source.
_usage_sources: dict[tuple[str, str], PlatformUsageSource] = {}
_usage_sources_lock = threading.Lock()


def get_usage_source(api_key: str, client: HexgateClient) -> PlatformUsageSource:
    """Get-or-create the shared source for this key and platform.

    Its executor's worker is joined at interpreter exit, so a hung background
    fetch can delay exit by up to the client's refresh timeout.
    """
    cache_key = (api_key, client.config.base_url)
    # Held across creation: each source owns an executor thread, and a lost race
    # would leak one.
    with _usage_sources_lock:
        source = _usage_sources.get(cache_key)
        if source is None:
            source = _usage_sources[cache_key] = PlatformUsageSource(
                fetcher=client,
                executor=ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix=_EXECUTOR_THREAD_PREFIX
                ),
                settings=UsageRefreshSettings.from_env(),
                clock=_monotonic,
            )
        return source


def resolve_usage_source(
    *,
    api_key: str | None = None,
    client: HexgateClient | None = None,
) -> PlatformUsageSource | None:
    """The shared source, or ``None`` when there is no platform to ask:
    HEXGATE_LOCAL_MODE, HEXGATE_LOCAL_POLICY, or no key."""
    if _local_mode_active() or os.environ.get(_LOCAL_POLICY_ENV_VAR):
        return None
    key = resolve_api_key(api_key)
    if not key:
        return None
    if client is None:
        from hexgate.cloud.client import HexgateClient, HexgateConfig

        client = HexgateClient(HexgateConfig.from_env(api_key=key))
    return get_usage_source(key, client)
