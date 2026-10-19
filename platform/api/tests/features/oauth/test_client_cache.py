"""The client metadata cache and the redirect_uri decision.

The fetch is faked through ``client_cache.fetch_client_metadata`` and time
through the cache's ``clock``, so each test states what each client_id serves
and when.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.shared.auth import InvalidRedirectUriError, InvalidScopeError
from pydantic import AnyUrl

from hexgate_api.features.oauth import client_cache
from hexgate_api.features.oauth.client_cache import (
    DOCUMENT_TTL_SECONDS,
    FAILURE_TTL_SECONDS,
    MAX_CLIENT_ID_LENGTH,
    MAX_ENTRIES,
    MAX_STALE_SECONDS,
    ClientMetadata,
    ClientMetadataCache,
)
from hexgate_api.features.oauth.client_metadata import ClientMetadataError
from tests.features.usage.fakes import FakeClock

CLIENT_ID = "https://client.example/oauth/metadata.json"


def _document(client_id: str = CLIENT_ID, **fields) -> dict:
    return {"client_id": client_id, "redirect_uris": ["http://localhost/cb"], **fields}


def _fails(client_id: str) -> dict:
    raise ClientMetadataError("client_id answered HTTP 404")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def cache(clock) -> ClientMetadataCache:
    return ClientMetadataCache(clock=clock)


class _Fetches:
    """The fake fetch's log; set ``gate`` to hold every fetch until it is set."""

    def __init__(self) -> None:
        self.client_ids: list[str] = []
        self.insecure_flags: list[bool] = []
        self.gate: asyncio.Event | None = None


@pytest.fixture
def serves(monkeypatch):
    """Answer each fetch with ``answer(client_id)``; returns the fetch log."""

    def install(answer) -> _Fetches:
        fetches = _Fetches()

        async def fake_fetch(client_id, *, allow_insecure_localhost=False):
            fetches.client_ids.append(client_id)
            fetches.insecure_flags.append(allow_insecure_localhost)
            if fetches.gate is not None:
                await fetches.gate.wait()
            return answer(client_id)

        monkeypatch.setattr(client_cache, "fetch_client_metadata", fake_fetch)
        return fetches

    return install


async def test_get_happy_path(cache, serves):
    fetches = serves(_document)

    metadata = await cache.get(CLIENT_ID)

    assert metadata.client_id == CLIENT_ID
    assert metadata.redirect_uris == [AnyUrl("http://localhost/cb")]
    assert fetches.insecure_flags == [False]


async def test_when_document_names_another_client_id_then_refused(cache, serves):
    serves(lambda _: _document("https://client.example/other.json"))

    with pytest.raises(ClientMetadataError, match="does not match"):
        await cache.get(CLIENT_ID)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"redirect_uris": None}, "no redirect_uris"),
        ({"redirect_uris": []}, "invalid fields: redirect_uris"),
        ({"redirect_uris": ["not a url"]}, "invalid fields: redirect_uris"),
        ({"token_endpoint_auth_method": "magic"}, "token_endpoint_auth_method"),
    ],
    ids=["null-redirects", "no-redirects", "bad-redirect", "bad-auth-method"],
)
async def test_when_document_invalid_then_refused(cache, serves, fields, message):
    serves(lambda _: _document(**fields))

    with pytest.raises(ClientMetadataError, match=message):
        await cache.get(CLIENT_ID)


async def test_when_client_id_too_long_then_refused_without_caching(cache, serves):
    fetches = serves(_fails)
    longest = "https://client.example/" + "a" * (MAX_CLIENT_ID_LENGTH - 23)

    with pytest.raises(ClientMetadataError, match="longer than"):
        await cache.get(longest + "a")
    with pytest.raises(ClientMetadataError, match="HTTP 404"):
        await cache.get(longest)

    assert fetches.client_ids == [longest]
    assert list(cache._failures._entries) == [longest]


async def test_when_cached_then_not_fetched_again(cache, serves, clock):
    fetches = serves(_document)

    await cache.get(CLIENT_ID)
    clock.now = DOCUMENT_TTL_SECONDS - 1
    await cache.get(CLIENT_ID)

    assert fetches.client_ids == [CLIENT_ID]


async def test_when_an_hour_passes_then_fetched_again(cache, serves, clock):
    fetches = serves(_document)

    await cache.get(CLIENT_ID)
    clock.now = DOCUMENT_TTL_SECONDS
    await cache.get(CLIENT_ID)

    assert fetches.client_ids == [CLIENT_ID, CLIENT_ID]


async def test_when_fetch_fails_then_failure_cached_for_a_minute(cache, serves, clock):
    fetches = serves(_fails)

    for now in (0, FAILURE_TTL_SECONDS - 1, FAILURE_TTL_SECONDS):
        clock.now = now
        with pytest.raises(ClientMetadataError, match="HTTP 404"):
            await cache.get(CLIENT_ID)

    assert fetches.client_ids == [CLIENT_ID, CLIENT_ID]


async def test_when_check_fails_then_failure_cached(cache, serves):
    fetches = serves(lambda _: _document("https://client.example/other.json"))

    for _ in range(2):
        with pytest.raises(ClientMetadataError, match="does not match"):
            await cache.get(CLIENT_ID)

    assert fetches.client_ids == [CLIENT_ID]


class _Outage:
    """Serves the document until ``down`` is set, then fails every fetch."""

    def __init__(self) -> None:
        self.down = False

    def __call__(self, client_id: str) -> dict:
        if self.down:
            raise ClientMetadataError("client metadata fetch timed out") from (
                TimeoutError()
            )
        return _document()


async def test_when_refetch_fails_then_expired_document_served(
    cache, serves, clock, caplog
):
    outage = _Outage()
    fetches = serves(outage)
    first = await cache.get(CLIENT_ID)
    outage.down = True

    served = []
    for now in (
        DOCUMENT_TTL_SECONDS,
        DOCUMENT_TTL_SECONDS + FAILURE_TTL_SECONDS - 1,
        DOCUMENT_TTL_SECONDS + FAILURE_TTL_SECONDS,
    ):
        clock.now = now
        served.append(await cache.get(CLIENT_ID))

    assert all(doc is first for doc in served)
    assert len(fetches.client_ids) == 3
    assert caplog.messages[-1] == (
        f"serving stale OAuth client metadata for {CLIENT_ID}: "
        "refetch failed (TimeoutError)"
    )


async def test_when_stale_for_an_hour_then_refused(cache, serves, clock):
    outage = _Outage()
    fetches = serves(outage)
    await cache.get(CLIENT_ID)
    outage.down = True
    clock.now = DOCUMENT_TTL_SECONDS
    await cache.get(CLIENT_ID)
    # A re-arm just before the limit must not move the limit.
    clock.now = DOCUMENT_TTL_SECONDS + MAX_STALE_SECONDS - 1
    await cache.get(CLIENT_ID)

    clock.now += FAILURE_TTL_SECONDS
    for _ in range(2):
        with pytest.raises(ClientMetadataError, match="timed out"):
            await cache.get(CLIENT_ID)

    assert len(fetches.client_ids) == 4


async def test_when_refetch_recovers_then_new_document_served(cache, serves, clock):
    outage = _Outage()
    serves(outage)
    first = await cache.get(CLIENT_ID)
    outage.down = True
    clock.now = DOCUMENT_TTL_SECONDS
    await cache.get(CLIENT_ID)

    outage.down = False
    clock.now = DOCUMENT_TTL_SECONDS + FAILURE_TTL_SECONDS

    assert await cache.get(CLIENT_ID) is not first


async def test_when_failures_flood_then_cached_documents_kept(cache, serves):
    fetches = serves(lambda cid: _document() if cid == CLIENT_ID else _fails(cid))
    await cache.get(CLIENT_ID)

    for i in range(MAX_ENTRIES + 1):
        with pytest.raises(ClientMetadataError):
            await cache.get(f"https://bad.example/{i}")
    await cache.get(CLIENT_ID)

    assert fetches.client_ids.count(CLIENT_ID) == 1


async def test_when_513th_client_then_least_recently_used_evicted(cache, serves):
    fetches = serves(_document)
    client_ids = [f"https://client.example/{i}" for i in range(MAX_ENTRIES + 1)]
    for client_id in client_ids[:MAX_ENTRIES]:
        await cache.get(client_id)
    await cache.get(client_ids[0])  # now the most recently used

    await cache.get(client_ids[MAX_ENTRIES])
    fetches.client_ids.clear()
    await cache.get(client_ids[0])
    await cache.get(client_ids[2])
    await cache.get(client_ids[1])

    assert fetches.client_ids == [client_ids[1]]


async def test_when_concurrent_misses_then_one_fetch(cache, serves):
    fetches = serves(_document)
    fetches.gate = asyncio.Event()
    waiters = [asyncio.create_task(cache.get(CLIENT_ID)) for _ in range(2)]
    while not fetches.client_ids:  # until the fetch is held at the gate
        await asyncio.sleep(0)
    waiters.append(asyncio.create_task(cache.get(CLIENT_ID)))
    await asyncio.sleep(0)
    fetches.gate.set()

    results = await asyncio.gather(*waiters)

    assert fetches.client_ids == [CLIENT_ID]
    assert results[0] is results[1] is results[2]


async def test_when_caller_cancelled_then_fetch_still_cached(cache, serves):
    fetches = serves(_document)
    fetches.gate = asyncio.Event()
    caller = asyncio.create_task(cache.get(CLIENT_ID))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    in_flight = cache._in_flight[CLIENT_ID]
    fetches.gate.set()
    await in_flight  # the shared fetch finishes with no caller waiting on it

    await cache.get(CLIENT_ID)

    assert fetches.client_ids == [CLIENT_ID]


async def test_when_insecure_localhost_allowed_then_passed_to_fetch(serves):
    fetches = serves(_document)

    await ClientMetadataCache(allow_insecure_localhost=True).get(
        "http://localhost:8000/client.json"
    )

    assert fetches.insecure_flags == [True]


def _client(*redirect_uris: str) -> ClientMetadata:
    return ClientMetadata(client_id=CLIENT_ID, redirect_uris=list(redirect_uris))


def _allows(client: ClientMetadata, redirect_uri: str) -> bool:
    try:
        client.validate_redirect_uri(AnyUrl(redirect_uri))
    except InvalidRedirectUriError:
        return False
    return True


def test_validate_redirect_uri_happy_path():
    client = _client("https://app.example/cb", "https://app.example/other")

    assert client.validate_redirect_uri(AnyUrl("https://app.example/other")) == (
        AnyUrl("https://app.example/other")
    )


def test_when_redirect_uri_spelled_differently_then_accepted():
    """Pydantic normalises both sides, so this is the URL the login goes to."""
    assert _allows(_client("https://app.example/cb"), "https://APP.example:443/cb")


@pytest.mark.parametrize(
    "requested",
    [
        "https://app.example/cb/",
        "https://app.example/cb?x=1",
        "https://app.example/cb?x=",
        "https://app.example:8443/cb",
        "https://app.example/c",
        "https://evil.example/cb",
    ],
)
def test_when_redirect_uri_not_listed_then_refused(requested):
    assert not _allows(_client("https://app.example/cb"), requested)


@pytest.mark.parametrize(
    ("registered", "requested"),
    [
        ("http://localhost/callback", "http://localhost:53124/callback"),
        ("http://127.0.0.1/callback", "http://127.0.0.1:8080/callback"),
        ("http://[::1]/callback", "http://[::1]:8080/callback"),
        ("http://127.0.0.1:9000/callback", "http://127.0.0.1:8080/callback"),
        ("http://127.0.0.1:9000/callback", "http://127.0.0.1/callback"),
    ],
)
def test_when_loopback_on_another_port_then_accepted(registered, requested):
    assert _allows(_client(registered), requested)


@pytest.mark.parametrize(
    ("registered", "requested"),
    [
        ("http://localhost/callback", "http://localhost:53124/other"),
        ("http://localhost/callback", "http://localhost:53124/callback?x=1"),
        ("http://localhost/callback", "http://localhost:53124/callback#x"),
        ("http://localhost/callback", "http://127.0.0.1:53124/callback"),
        ("http://localhost/callback", "https://localhost:53124/callback"),
        ("https://localhost/callback", "https://localhost:53124/callback"),
        ("http://localhost/callback", "http://evil@localhost:53124/callback"),
        ("http://evil@localhost/callback", "http://evil@localhost:53124/callback"),
        ("http://:pw@localhost/callback", "http://:pw@localhost:53124/callback"),
        ("http://app.example/callback", "http://app.example:8080/callback"),
        ("http://localhost.evil.example/cb", "http://localhost.evil.example:1/cb"),
    ],
)
def test_when_port_differs_outside_loopback_rule_then_refused(registered, requested):
    assert not _allows(_client(registered), requested)


def test_validate_scope_happy_path():
    """The document lists no scope, as Claude Code's doesn't; the registry decides."""
    client = _client("http://localhost/cb")

    assert client.validate_scope("policy:read audit:read") == [
        "policy:read",
        "audit:read",
    ]


@pytest.mark.parametrize(
    "requested", ["admin", "policy:read admin", "", "policy:read  audit:read"]
)
def test_when_scope_not_in_registry_then_refused(requested):
    client = ClientMetadata(
        client_id=CLIENT_ID, redirect_uris=["http://localhost/cb"], scope="admin"
    )

    with pytest.raises(InvalidScopeError):
        client.validate_scope(requested)


def test_when_no_scope_then_none_as_in_the_sdk():
    assert _client("http://localhost/cb").validate_scope(None) is None


def test_when_no_redirect_uri_then_sole_registered_one_used():
    client = _client("http://localhost/cb")

    assert client.validate_redirect_uri(None) == AnyUrl("http://localhost/cb")
