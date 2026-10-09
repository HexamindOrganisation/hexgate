import asyncio

import httpx
import pytest
import respx

from hexgate_mcp.jwks import JwksCache
from tests.conftest import JWKS_URL, SigningKey, jwks


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def cache(clock: Clock):
    async with httpx.AsyncClient() as http:
        yield JwksCache(JWKS_URL, http, clock)


@respx.mock
async def test_get_happy_path(cache, key):
    respx.get(JWKS_URL).respond(json=jwks(key))

    found = await cache.get(key.kid)

    assert found is not None
    assert found.key_id == key.kid


@respx.mock
async def test_when_kid_is_unknown_then_one_refetch_picks_up_rotated_key(
    cache, key, clock
):
    rotated = SigningKey("key-2")
    route = respx.get(JWKS_URL)
    route.side_effect = [
        httpx.Response(200, json=jwks(key)),
        httpx.Response(200, json=jwks(rotated)),
    ]
    assert await cache.get(key.kid) is not None
    clock.now += 31

    assert await cache.get(rotated.kid) is not None
    assert route.call_count == 2
    # Replaced, not merged: the key the API dropped is gone.
    assert await cache.get(key.kid) is None


@respx.mock
async def test_when_unknown_kid_within_30s_then_no_second_refetch(cache, key, clock):
    route = respx.get(JWKS_URL).respond(json=jwks(key))
    assert await cache.get(key.kid) is not None

    clock.now += 29
    assert await cache.get("forged-1") is None
    assert await cache.get("forged-2") is None
    assert route.call_count == 1

    clock.now += 1
    assert await cache.get("forged-3") is None
    assert route.call_count == 2


@respx.mock
async def test_when_concurrent_unknown_kids_then_one_fetch(cache, key):
    async def slow_jwks(_):
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=jwks(key))

    route = respx.get(JWKS_URL).mock(side_effect=slow_jwks)
    kids = [f"forged-{i}" for i in range(20)] + [key.kid]

    results = await asyncio.gather(*(cache.get(kid) for kid in kids))

    assert route.call_count == 1
    # The real key, asked for while the fetch was in flight, still resolves.
    assert results[-1] is not None
    assert all(r is None for r in results[:-1])


@respx.mock
async def test_when_cache_is_ten_minutes_old_then_refetched(cache, key, clock):
    route = respx.get(JWKS_URL).respond(json=jwks(key))
    await cache.get(key.kid)

    clock.now += 599
    await cache.get(key.kid)
    assert route.call_count == 1

    clock.now += 1
    await cache.get(key.kid)
    assert route.call_count == 2


@respx.mock
async def test_when_jwks_fetch_fails_then_stale_keys_serve_and_retry_waits_30s(
    cache, key, clock
):
    route = respx.get(JWKS_URL)
    route.side_effect = [
        httpx.Response(200, json=jwks(key)),
        httpx.Response(503),
        httpx.Response(200, json=jwks(key)),
    ]
    await cache.get(key.kid)
    clock.now += 600

    assert await cache.get(key.kid) is not None
    assert await cache.get(key.kid) is not None
    assert route.call_count == 2

    clock.now += 30
    assert await cache.get(key.kid) is not None
    assert route.call_count == 3


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(httpx.Response(200, text="<html>"), id="not-json"),
        pytest.param(httpx.Response(200, json={"nope": []}), id="no-keys-list"),
        pytest.param(httpx.Response(200, json=[]), id="not-an-object"),
    ],
)
@respx.mock
async def test_when_jwks_body_is_malformed_then_no_key(cache, key, body):
    respx.get(JWKS_URL).mock(return_value=body)

    assert await cache.get(key.kid) is None


@respx.mock
async def test_when_jwks_has_unusable_entries_then_valid_key_still_loads(cache, key):
    document = jwks(key)
    document["keys"] += [{"kty": "OKP", "crv": "Ed25519"}, {"kid": "bad", "kty": "?"}]
    respx.get(JWKS_URL).respond(json=document)

    assert await cache.get(key.kid) is not None


@respx.mock
async def test_when_jwks_keys_have_no_kid_then_stale_keys_keep_serving(
    cache, key, clock, caplog
):
    no_kid = {k: v for k, v in key.jwk().items() if k != "kid"}
    route = respx.get(JWKS_URL)
    route.side_effect = [
        httpx.Response(200, json=jwks(key)),
        httpx.Response(200, json={"keys": [no_kid]}),
    ]
    await cache.get(key.kid)
    clock.now += 600

    assert await cache.get(key.kid) is not None
    assert route.call_count == 2
    assert "no key with a `kid`" in caplog.text
