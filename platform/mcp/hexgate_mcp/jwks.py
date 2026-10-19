"""The platform API's OAuth public key, fetched from its JWKS and cached.

The MCP server never has the private key; it only checks signatures with the
public one. Keys are cached for ten minutes. A `kid` not in the cache triggers
one refetch, so a rotated key is picked up without waiting for the cache to
expire; refetches (and retries after a failed fetch) are spaced at least 30 s
apart, so a flood of forged `kid`s costs one fetch per 30 s, not one per
request.
"""

import asyncio
import logging
import time
from collections.abc import Callable

import httpx
import jwt

logger = logging.getLogger(__name__)

JWKS_TTL_SECONDS = 600
REFETCH_INTERVAL_SECONDS = 30


class JwksCache:
    def __init__(
        self,
        jwks_url: str,
        http: httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._jwks_url = jwks_url
        self._http = http
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = float("-inf")  # last successful fetch
        self._attempted_at = float("-inf")  # last fetch, successful or not
        self._task: asyncio.Task[None] | None = None  # the one fetch in flight

    async def get(self, kid: str) -> jwt.PyJWK | None:
        if kid in self._keys:
            # A known key is served at once, even past its ten minutes: the
            # refetch runs in the background, so no request waits on the
            # network for a key it already has. If the API can't be reached,
            # the cached keys keep working: the API re-checks every token
            # anyway, and while it's down no call gets through.
            if self._expired() and self._refetch_allowed():
                self._refetch_task()
            return self._keys[kid]
        if self._refetch_allowed():
            # Every request waits on the same fetch, so concurrent misses cost
            # one. Shielded: a cancelled request leaves the fetch running for
            # the others instead of cancelling it.
            await asyncio.shield(self._refetch_task())
        return self._keys.get(kid)

    def _expired(self) -> bool:
        return self._clock() - self._fetched_at >= JWKS_TTL_SECONDS

    def _refetch_allowed(self) -> bool:
        return self._clock() - self._attempted_at >= REFETCH_INTERVAL_SECONDS

    def _refetch_task(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refetch())
        return self._task

    async def _refetch(self) -> None:
        try:
            keys = await self._fetch()
        except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
            logger.warning("JWKS fetch from %s failed: %s", self._jwks_url, exc)
            return
        finally:
            # Stamped when the fetch ends, not when it starts: a request that
            # arrives mid-fetch still sees a refetch as due, so it joins the
            # fetch in flight and reads the new keys instead of refusing.
            self._attempted_at = self._clock()
        # Replaced, not merged, so a key the API dropped stops verifying.
        self._keys = keys
        self._fetched_at = self._attempted_at

    async def _fetch(self) -> dict[str, jwt.PyJWK]:
        response = await self._http.get(self._jwks_url)
        response.raise_for_status()
        document = response.json()
        if not isinstance(document, dict):
            raise jwt.PyJWKSetError("JWKS document is not an object")
        # PyJWKSet skips entries it cannot use, and raises when none is left.
        key_set = jwt.PyJWKSet.from_dict(document)
        keys = {k.key_id: k for k in key_set.keys if k.key_id}
        if not keys:
            # Treated like an empty key list: tokens are matched to keys by
            # `kid`, so a key without one is useless.
            raise jwt.PyJWKSetError("JWKS has no key with a `kid`")
        return keys
