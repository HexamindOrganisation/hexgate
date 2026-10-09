"""The platform API's public signing keys, fetched from its JWKS and cached.

The server holds no key of its own. Keys are cached for ten minutes. A `kid`
not in the cache triggers one refetch, so a rotated key is picked up without
waiting for the cache to expire; refetches (and retries after a failed fetch)
are spaced at least 30 s apart, so a flood of forged `kid`s costs one fetch
per 30 s, not one per request.
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
        self._lock = asyncio.Lock()

    async def get(self, kid: str) -> jwt.PyJWK | None:
        if self._needs_refetch(kid):
            async with self._lock:
                # Re-checked under the lock: requests that queued behind a
                # refetch find it done and must not start another.
                if self._needs_refetch(kid):
                    await self._refetch()
        # Stale keys keep serving while the JWKS is unreachable: the API, which
        # makes the real check, is then unreachable too.
        return self._keys.get(kid)

    def _needs_refetch(self, kid: str) -> bool:
        now = self._clock()
        missing_or_expired = (
            kid not in self._keys or now - self._fetched_at >= JWKS_TTL_SECONDS
        )
        return (
            missing_or_expired and now - self._attempted_at >= REFETCH_INTERVAL_SECONDS
        )

    async def _refetch(self) -> None:
        try:
            keys = await self._fetch()
        except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
            logger.warning("JWKS fetch from %s failed: %s", self._jwks_url, exc)
            return
        finally:
            # Stamped when the fetch ends, not when it starts: a request that
            # arrives mid-fetch must still see a refetch as due, so it queues
            # on the lock and reads the new keys instead of refusing.
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
            # Same failure as no usable key: a token can only be matched by kid.
            raise jwt.PyJWKSetError("JWKS has no key with a `kid`")
        return keys
