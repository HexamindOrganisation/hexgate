"""The platform term of the usage namespaces (``agent_usage.*`` and ``user_usage.*``).

Keeps one snapshot per usage scope, fetched by that scope kind's fetcher (an agent's
is ``GET /v1/agents/{name}/usage``, a user's ``POST /v1/usage/users:read``), refreshed
stale-while-revalidate, and combines it with the process-local ledger into the
namespace a decision reads. Shared per ``(api_key, base_url)`` like
:class:`~hexgate.security.bans.PlatformBanSource`, and fail-soft: a platform blip
degrades to ledger-only values by default, never to an error. User states are held
in an LRU (``HEXGATE_USAGE_USER_CACHE``) and back off together, and log lines print a
user as a fingerprint. Inert until the run boundary calls it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, StrEnum, auto
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from hexgate.cloud.client import HexgateError
from hexgate.config.env import resolve_api_key
from hexgate.runtime.agent_usage import (
    Clock,
    OnUnavailable,
    UsageLedger,
    UsageScope,
    UsageScopeKind,
    _monotonic,
    combined_namespace,
    ledger_namespace,
    user_cache_size_from_env,
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

# Users come and go, so a background refresh every 5 s buys nothing; the sync and
# staleness thresholds are the agents'.
DEFAULT_USER_MIN_REFRESH_SECONDS: Final[float] = 30.0

# The platform's read and ingest cap on user_id, in characters, not bytes.
USER_ID_MAX_LENGTH: Final = 256
_USER_ID_ENCODING: Final = "utf-8"

_VALUES_KEY: Final = "values"
_NOT_FOUND: Final = 404
_METHOD_NOT_ALLOWED: Final = 405
_UNPROCESSABLE: Final = 422
# Answers a retry won't change soon: unregistered scope, bad or unscoped key.
_REJECTED_STATUSES: Final = frozenset({401, 403, _NOT_FOUND})
# A platform without the user read: older releases' GET-only SPA catch-all
# answers a POST to an unknown /v1 path with 405, not 404.
_ROUTE_MISSING_STATUSES: Final = frozenset({_NOT_FOUND, _METHOD_NOT_ALLOWED})
# Every ID the platform would refuse is caught before sending, so a 422 refuses
# the request's shape (paths, body fields), which every user shares.
_KIND_REJECTED_STATUSES: Final = (
    _REJECTED_STATUSES | _ROUTE_MISSING_STATUSES | {_UNPROCESSABLE}
)
REJECTED_RETRY_SECONDS: Final[float] = 300.0

# User log lines carry the fingerprinted scope, the exception class and the
# status, never the exception's message.
_KIND_PAUSED_MESSAGE: Final = (
    "usage refresh for %s failed (%s, status %s); "
    "pausing every %s refresh for %.0f s, using last-good"
)
_ROUTE_MISSING_MESSAGE: Final = (
    "this platform has no %s usage read; user_usage.* reads local usage only"
)
_KEY_UNREADABLE_MESSAGE: Final = (
    f"%s is not a key the platform can read (over {USER_ID_MAX_LENGTH} characters "
    "or not valid UTF-8), so it is never sent; such keys read as unavailable"
)
_EXECUTOR_THREAD_PREFIX: Final = "hexgate-usage"
# Single-flight per scope already, so workers only keep one slow scope from
# delaying another's refresh.
_EXECUTOR_MAX_WORKERS: Final = 4


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
class RefreshEnv:
    """The variables one scope kind's thresholds are read from."""

    min_refresh: str
    sync_after: str
    max_staleness: str


AGENT_REFRESH_ENV: Final = RefreshEnv(
    _MIN_REFRESH_ENV, _SYNC_AFTER_ENV, _MAX_STALENESS_ENV
)


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
    def from_env(
        cls,
        environ: Mapping[str, str] = os.environ,
        *,
        env: RefreshEnv = AGENT_REFRESH_ENV,
        defaults: UsageRefreshSettings | None = None,
    ) -> UsageRefreshSettings:
        """Read the thresholds from ``env`` (the agents' ``HEXGATE_USAGE_*`` when
        omitted). A bad value logs and falls back to ``defaults``: it runs at the
        run boundary and must not stop agents."""
        defaults = cls() if defaults is None else defaults
        try:
            return cls(
                min_refresh_seconds=float(
                    environ.get(env.min_refresh, defaults.min_refresh_seconds)
                ),
                sync_after_seconds=float(
                    environ.get(env.sync_after, defaults.sync_after_seconds)
                ),
                max_staleness_seconds=float(
                    environ.get(env.max_staleness, defaults.max_staleness_seconds)
                ),
            )
        except ValueError as exc:
            logger.warning("ignoring usage refresh settings, using defaults: %s", exc)
            return defaults


USER_REFRESH_ENV: Final = RefreshEnv(
    "HEXGATE_USAGE_USER_REFRESH_SECONDS",
    "HEXGATE_USAGE_USER_SYNC_AFTER_SECONDS",
    "HEXGATE_USAGE_USER_MAX_STALENESS_SECONDS",
)
USER_REFRESH_DEFAULTS: Final = UsageRefreshSettings(
    min_refresh_seconds=DEFAULT_USER_MIN_REFRESH_SECONDS
)


def user_id_is_readable(user_id: str) -> bool:
    """Whether the platform's user read accepts ``user_id``. A refused one is a
    422 per run that teaches nothing, so it is never sent."""
    if len(user_id) > USER_ID_MAX_LENGTH:
        return False
    try:
        user_id.encode(_USER_ID_ENCODING)
    except UnicodeEncodeError:  # a lone surrogate
        return False
    return True


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


# Fetches one scope's values: (key, sorted paths) -> the response body.
ScopeFetcher = Callable[[str, Sequence[str]], dict[str, Any]]


class _Tier(Enum):
    NONE = auto()
    BACKGROUND = auto()
    SYNC = auto()


class _Notice(StrEnum):
    """What a kind reports once per source, however many scopes hit it."""

    ROUTE_MISSING = auto()
    KEY_UNREADABLE = auto()


# A notice logged once per source: an agent's 404 per scope, the others per kind.
_ReportKey = UsageScope | tuple[UsageScopeKind, _Notice]


@dataclass(frozen=True, slots=True)
class _Attempt:
    at: float
    paths: frozenset[str]


def _any_key(_key: str) -> bool:
    return True


@dataclass(frozen=True, slots=True)
class ScopeKindSpec:
    """How the source fetches and keeps one scope kind."""

    fetch: ScopeFetcher
    settings: UsageRefreshSettings
    # None keeps every scope for good: a process has a handful of agents.
    capacity: int | None = None
    # When most fetches are for a key never seen before, a per-scope pause never
    # kicks in, so one failure pauses the whole kind.
    shared_backoff: bool = False
    # Keys the platform would refuse are never sent: they read UNAVAILABLE.
    accepts: Callable[[str], bool] = _any_key


@dataclass(slots=True, eq=False)
class _ScopeState:
    """Everything the source knows about one scope. Mutated under the source's
    state lock, except ``fetch_lock``, which serialises this scope's fetches."""

    fetch_lock: threading.Lock = field(default_factory=threading.Lock)
    snapshot: UsageSnapshot | None = None
    # Every path any caller has asked for, read when a fetch runs, so callers
    # wanting different paths share one snapshot instead of evicting each other.
    wanted: frozenset[str] = frozenset()
    last_attempt: _Attempt | None = None
    rejected_until: float = float("-inf")


class _ScopeTable:
    """One kind's scope states. A bounded table drops its least recently used
    scope past capacity. Called under the source's state lock only."""

    def __init__(self, capacity: int | None) -> None:
        self._capacity = capacity
        self._states: OrderedDict[UsageScope, _ScopeState] = OrderedDict()

    def __len__(self) -> int:
        return len(self._states)

    def get(self, scope: UsageScope) -> _ScopeState | None:
        return self._states.get(scope)

    def touch(self, scope: UsageScope) -> _ScopeState:
        """``scope``'s state, created if new, now the most recently used."""
        state = self._states.get(scope)
        if state is None:
            state = self._states[scope] = _ScopeState()
            if self._capacity is not None and len(self._states) > self._capacity:
                self._states.popitem(last=False)
        else:
            self._states.move_to_end(scope)
        return state


class PlatformUsageSource:
    """Per-scope usage snapshots: refreshed at the run boundary, read per decision.

    ``refresh`` may block on one bounded fetch when the snapshot is cold or older
    than ``sync_after``; otherwise it schedules at most one background fetch per
    scope. ``read`` never touches the network. Fetches keep last-good on failure.

    Each kind has its own thresholds. An agent's failed fetch is not retried within
    ``min_refresh`` unless the path set grew, and a 401, 403 or 404 is not retried
    within ``REJECTED_RETRY_SECONDS`` at all. Users come and go, so their states sit
    in an LRU, and one user's failure pauses every user's fetch. A user ID the
    platform would refuse is never sent. A scope whose kind has no spec never
    fetches, so it reads ``UNAVAILABLE``. Log lines print a user scope as a
    fingerprint, and never a user failure's message.
    """

    def __init__(
        self,
        kinds: Mapping[UsageScopeKind, ScopeKindSpec],
        executor: Executor,
        clock: Clock,
    ) -> None:
        self._kinds = dict(kinds)
        self._executor = executor
        self._clock = clock
        # Held only for state reads and writes, never across HTTP.
        self._state_lock = threading.Lock()
        self._tables: dict[UsageScopeKind, _ScopeTable] = {
            kind: _ScopeTable(spec.capacity) for kind, spec in self._kinds.items()
        }
        self._paused_until: dict[UsageScopeKind, float] = {}
        self._in_flight: set[UsageScope] = set()
        self._reported: set[_ReportKey] = set()

    def refresh(self, scope: UsageScope, paths: frozenset[str]) -> None:
        state = self._remember(scope, paths) if paths else None
        if state is None:
            return
        tier = self._tier(scope, state, paths)
        if tier is _Tier.SYNC:
            self._fetch_now(scope, state)
        elif tier is _Tier.BACKGROUND:
            self._schedule(scope)

    async def refresh_async(self, scope: UsageScope, paths: frozenset[str]) -> None:
        """As :meth:`refresh`; only the synchronous fetch leaves the loop."""
        state = self._remember(scope, paths) if paths else None
        if state is None:
            return
        tier = self._tier(scope, state, paths)
        if tier is _Tier.SYNC:
            await asyncio.to_thread(self._fetch_now, scope, state)
        elif tier is _Tier.BACKGROUND:
            self._schedule(scope)

    def read(
        self,
        scope: UsageScope,
        paths: frozenset[str],
        ledger: UsageLedger,
        *,
        on_unavailable: OnUnavailable = OnUnavailable.ALLOW,
        current_run_age: float | None = None,
    ) -> UsageReading:
        """The usage namespace for ``paths``. Anything short of ``FRESH`` asks for
        a background refresh, so a long run heals its own snapshot. The ask is
        dropped for a kind with no spec, while one is in flight, or while the
        scope is throttled."""
        state = self._remember(scope, paths)
        snapshot = None if state is None else state.snapshot
        if (
            snapshot is not None
            and (age := self._clock() - snapshot.fetched_at)
            < self._kinds[scope.kind].settings.max_staleness_seconds
        ):
            reading = self._combine(
                scope, snapshot, age, paths, ledger, on_unavailable, current_run_age
            )
        else:
            reading = UsageReading(
                _fallback(ledger, paths, on_unavailable, current_run_age),
                UsageState.UNAVAILABLE,
            )
        if reading.state is not UsageState.FRESH:
            self._schedule(scope)
        return reading

    def _combine(
        self,
        scope: UsageScope,
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
            current_run_age=current_run_age,
        )
        if missing:
            # Under DENY a missing path stays absent, so only its constraints fail
            # closed; the rest of the policy keeps working (F5).
            namespace |= (
                _fallback(ledger, missing, on_unavailable, current_run_age) or {}
            )
            state = UsageState.PARTIAL
        elif age >= self._kinds[scope.kind].settings.sync_after_seconds:
            state = UsageState.STALE
        else:
            state = UsageState.FRESH
        return UsageReading(namespace, state)

    def _remember(self, scope: UsageScope, paths: frozenset[str]) -> _ScopeState | None:
        """``scope``'s state, now the most recently used, with ``paths`` wanted.
        ``None``, and nothing kept, for a scope the source never fetches."""
        if not self._fetchable(scope):
            if scope.kind in self._kinds:
                self._report_once(
                    (scope.kind, _Notice.KEY_UNREADABLE), _KEY_UNREADABLE_MESSAGE, scope
                )
            return None
        with self._state_lock:
            state = self._tables[scope.kind].touch(scope)
            state.wanted |= paths
            return state

    def _fetchable(self, scope: UsageScope) -> bool:
        spec = self._kinds.get(scope.kind)
        return spec is not None and spec.accepts(scope.key)

    def _tier(
        self, scope: UsageScope, state: _ScopeState, paths: frozenset[str]
    ) -> _Tier:
        with self._state_lock:
            # A cold user during a pause doesn't even leave the loop.
            if self._kind_paused_locked(scope.kind):
                return _Tier.NONE
            snapshot = state.snapshot
        settings = self._kinds[scope.kind].settings
        if snapshot is None:
            return _Tier.SYNC
        age = self._clock() - snapshot.fetched_at
        if age >= settings.sync_after_seconds:
            return _Tier.SYNC
        # A new path never blocks the run: it reads ledger-only until this lands (F5).
        if not paths <= snapshot.values.keys() or age >= settings.min_refresh_seconds:
            return _Tier.BACKGROUND
        return _Tier.NONE

    def _fetch_now(self, scope: UsageScope, state: _ScopeState) -> None:
        with state.fetch_lock:
            if self._throttled(scope, state) or self._covered(scope, state):
                return
            self._fetch(scope, state)

    def _schedule(self, scope: UsageScope) -> None:
        if not self._fetchable(scope):
            return
        with self._state_lock:
            state = self._tables[scope.kind].get(scope)
            if (
                state is None
                or scope in self._in_flight
                or self._throttled_locked(scope, state)
            ):
                return
            self._in_flight.add(scope)
        try:
            # The state, not the key: a job outliving its scope's eviction writes
            # to the orphan, which is never re-inserted.
            future = self._executor.submit(self._fetch_in_background, scope, state)
        except RuntimeError as exc:  # executor shut down at interpreter exit
            logger.debug("usage refresh for %s not scheduled: %s", scope, exc)
            self._finish(scope)
            return
        future.add_done_callback(lambda _done: self._finish(scope))

    def _fetch_in_background(self, scope: UsageScope, state: _ScopeState) -> None:
        with state.fetch_lock:
            if not self._throttled(scope, state):
                self._fetch(scope, state)

    def _finish(self, scope: UsageScope) -> None:
        with self._state_lock:
            self._in_flight.discard(scope)

    def _fetch(self, scope: UsageScope, state: _ScopeState) -> None:
        """One fetch under ``state``'s fetch lock. Never raises: last-good stays."""
        spec = self._kinds[scope.kind]
        with self._state_lock:
            wanted = state.wanted
            started = self._clock()
            state.last_attempt = _Attempt(started, wanted)
        if not wanted:
            return
        try:
            payload = spec.fetch(scope.key, sorted(wanted))
            values = usage_values_from_payload(payload, wanted)
        except Exception as exc:  # noqa: BLE001 — fail-soft: keep last-good
            status = exc.status if isinstance(exc, HexgateError) else None
            if spec.shared_backoff:
                self._pause_kind(scope, spec.settings, started, exc, status)
            else:
                self._back_off_scope(scope, state, started, exc, status)
            return
        with self._state_lock:
            state.snapshot = UsageSnapshot(MappingProxyType(values), started)

    def _back_off_scope(
        self,
        scope: UsageScope,
        state: _ScopeState,
        started: float,
        exc: Exception,
        status: int | None,
    ) -> None:
        if isinstance(exc, UsageContentError):
            logger.error("usage body for %s rejected; using last-good: %s", scope, exc)
            return
        if status in _REJECTED_STATUSES:
            with self._state_lock:
                state.rejected_until = started + REJECTED_RETRY_SECONDS
        if status == _NOT_FOUND:
            self._log_unregistered(scope)
        else:
            logger.warning(
                "usage refresh for %s failed; using last-good: %s", scope, exc
            )

    def _pause_kind(
        self,
        scope: UsageScope,
        settings: UsageRefreshSettings,
        started: float,
        exc: Exception,
        status: int | None,
    ) -> None:
        """Back off a kind whose scopes are mostly cold. Logs the exception class
        and status, never its message: that may carry the key."""
        seconds = (
            REJECTED_RETRY_SECONDS
            if status in _KIND_REJECTED_STATUSES
            else settings.min_refresh_seconds
        )
        with self._state_lock:
            began = not self._kind_paused_locked(scope.kind)
            self._paused_until[scope.kind] = max(
                self._paused_until.get(scope.kind, float("-inf")), started + seconds
            )
        if status in _ROUTE_MISSING_STATUSES:
            self._report_once(
                (scope.kind, _Notice.ROUTE_MISSING), _ROUTE_MISSING_MESSAGE, scope.kind
            )
        elif began:
            logger.warning(
                _KIND_PAUSED_MESSAGE,
                scope,
                type(exc).__name__,
                status,
                scope.kind,
                seconds,
            )

    def _report_once(self, key: _ReportKey, message: str, *args: object) -> None:
        with self._state_lock:
            if key in self._reported:
                return
            self._reported.add(key)
        logger.warning(message, *args)

    def _log_unregistered(self, scope: UsageScope) -> None:
        # Otherwise it reads as an outage forever, so say what it is, once.
        self._report_once(
            scope,
            "%s is not registered in this API key's project; "
            "agent_usage.* reads local usage only",
            scope,
        )

    def _kind_paused_locked(self, kind: UsageScopeKind) -> bool:
        return self._clock() < self._paused_until.get(kind, float("-inf"))

    def _throttled(self, scope: UsageScope, state: _ScopeState) -> bool:
        with self._state_lock:
            return self._throttled_locked(scope, state)

    def _throttled_locked(self, scope: UsageScope, state: _ScopeState) -> bool:
        # A new path does not lift a rejection: the platform refuses the scope.
        if self._kind_paused_locked(scope.kind) or self._clock() < state.rejected_until:
            return True
        last = state.last_attempt
        min_refresh = self._kinds[scope.kind].settings.min_refresh_seconds
        return (
            last is not None
            and self._clock() - last.at < min_refresh
            and state.wanted <= last.paths
        )

    def _covered(self, scope: UsageScope, state: _ScopeState) -> bool:
        """Another fetch finished while this one waited for the lock."""
        with self._state_lock:
            snapshot = state.snapshot
            wanted = state.wanted
        sync_after = self._kinds[scope.kind].settings.sync_after_seconds
        return (
            snapshot is not None
            and self._clock() - snapshot.fetched_at < sync_after
            and wanted <= snapshot.values.keys()
        )


def _fallback(
    ledger: UsageLedger,
    paths: frozenset[str],
    on_unavailable: OnUnavailable,
    current_run_age: float | None,
) -> dict[str, int] | None:
    """The values for ``paths`` the platform could not supply: ledger-only by
    default (G3), nothing under DENY."""
    if on_unavailable is OnUnavailable.DENY:
        return None
    return ledger_namespace(ledger, paths, current_run_age=current_run_age)


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

    Its executor's workers drain every queued fetch at interpreter exit, so exit
    can wait up to the refresh timeout per round of ``_EXECUTOR_MAX_WORKERS``
    queued scopes.
    """
    cache_key = (api_key, client.config.base_url)
    # Held across creation: each source owns an executor, and a lost race would
    # leak its threads.
    with _usage_sources_lock:
        source = _usage_sources.get(cache_key)
        if source is None:
            source = _usage_sources[cache_key] = PlatformUsageSource(
                kinds={
                    UsageScopeKind.AGENT: ScopeKindSpec(
                        client.get_agent_usage, UsageRefreshSettings.from_env()
                    ),
                    UsageScopeKind.USER: ScopeKindSpec(
                        client.read_user_usage,
                        UsageRefreshSettings.from_env(
                            env=USER_REFRESH_ENV, defaults=USER_REFRESH_DEFAULTS
                        ),
                        capacity=user_cache_size_from_env(),
                        shared_backoff=True,
                        accepts=user_id_is_readable,
                    ),
                },
                executor=ThreadPoolExecutor(
                    max_workers=_EXECUTOR_MAX_WORKERS,
                    thread_name_prefix=_EXECUTOR_THREAD_PREFIX,
                ),
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
