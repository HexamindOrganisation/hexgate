"""Cache OAuth client metadata documents and decide where a login may redirect.

A client's document is fetched once (``client_metadata.py``), checked, and
kept for ``DOCUMENT_TTL_SECONDS``; a failed fetch is kept for
``FAILURE_TTL_SECONDS``, so a caller repeating one bad ``client_id`` costs one
fetch a minute rather than one per request. The two are cached apart, each
capped at ``MAX_ENTRIES`` and evicting the least recently used, so a flood of
bad ``client_id``s never pushes a good document out. Concurrent misses on one
``client_id`` share a single fetch.

When a refetch fails after a document expired, the expired document is served
for ``FAILURE_TTL_SECONDS`` more and the fetch retried after that, for up to
``MAX_STALE_SECONDS`` past its expiry: a blip at the client's host (or a DNS
pool held by a flood of other lookups) must not refuse every login of a client
that was working a moment ago. The cost: a ``redirect_uri`` the client removed
stays allowed that much longer while its metadata URL fails to serve a valid
document.

A document is cached as ``ClientMetadata``, the MCP SDK's client type, whose
``validate_redirect_uri`` and ``validate_scope`` the SDK's authorize handler
calls itself. A ``redirect_uri`` must equal one the document lists; the only
exception is an ``http`` loopback redirect, which may name any port (RFC 8252
§7.3), since a native client picks its port when it starts. A scope must be in
the platform's registry, whatever the document says.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, NamedTuple

from mcp.shared.auth import (
    InvalidRedirectUriError,
    InvalidScopeError,
    OAuthClientInformationFull,
)
from pydantic import AnyUrl, ValidationError

from hexgate_api.constants import OAUTH_SCOPES
from hexgate_api.features.oauth.client_metadata import (
    LOOPBACK_HOSTS,
    ClientMetadataError,
    fetch_client_metadata,
)

logger = logging.getLogger(__name__)

DOCUMENT_TTL_SECONDS = 3600.0
FAILURE_TTL_SECONDS = 60.0
MAX_STALE_SECONDS = 3600.0
MAX_ENTRIES = 512
# client_id arrives unauthenticated, in a POST body nginx lets reach megabytes,
# and is kept as a cache key; past this it is refused without being cached.
MAX_CLIENT_ID_LENGTH = 2048


class ClientMetadata(OAuthClientInformationFull):
    """A client's metadata document; ``ClientMetadataCache`` checks it describes the URL it came from."""

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        """Return ``redirect_uri`` if the document allows it, else raise ``InvalidRedirectUriError``.

        Both sides are pydantic URLs, so they compare in normalised form (host
        lowercased, default port dropped), which is the URL the login is
        redirected to. A ``redirect_uri`` is required, even when the document
        lists only one: the MCP SDK would stand that one in and then redirect an
        error there, which RFC 6749 §4.1.2.1 forbids for a target the request
        did not supply.
        """
        if redirect_uri is None:
            raise InvalidRedirectUriError("redirect_uri is required")
        for registered in self.redirect_uris or ():
            if _redirect_matches(redirect_uri, registered):
                return redirect_uri
        raise InvalidRedirectUriError(
            f"Redirect URI '{redirect_uri}' not registered for client"
        )

    def validate_scope(self, requested_scope: str | None) -> list[str] | None:
        """Return the requested scopes if each is in ``OAUTH_SCOPES``, else raise ``InvalidScopeError``.

        The platform's registry decides, not the document's own ``scope``
        field, which most clients (Claude Code's among them) leave out. With
        no ``scope`` this returns None, as the MCP SDK does. A repeated scope is
        kept once, in the order first asked.
        """
        if requested_scope is None:
            return None
        scopes = list(dict.fromkeys(requested_scope.split(" ")))
        for scope in scopes:
            if scope not in OAUTH_SCOPES:
                raise InvalidScopeError(f"Unknown scope {scope!r}")
        return scopes


class _Document(NamedTuple):
    metadata: ClientMetadata
    stale_until: float  # served after a failed refetch until then, not after


class ClientMetadataCache:
    """Per-process cache of client metadata documents. Event-loop only: no lock."""

    def __init__(
        self,
        *,
        allow_insecure_localhost: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._allow_insecure_localhost = allow_insecure_localhost
        self._clock = clock
        self._documents = _ExpiringLru[_Document](clock)
        # The failure's message, raised afresh on each hit so tracebacks don't pile up.
        self._failures = _ExpiringLru[str](clock)
        self._in_flight: dict[str, asyncio.Task[ClientMetadata]] = {}

    async def get(self, client_id: str) -> ClientMetadata:
        """Return the checked document at ``client_id``.

        Raises ``ClientMetadataError`` when it cannot be fetched or does not
        describe ``client_id`` and no recently expired copy can stand in; the
        message is safe to show the user.
        """
        if len(client_id) > MAX_CLIENT_ID_LENGTH:
            raise ClientMetadataError(
                f"client_id is longer than {MAX_CLIENT_ID_LENGTH} characters"
            )
        cached = self._documents.get(client_id)
        if cached is not None:
            return cached.metadata
        failure = self._failures.get(client_id)
        if failure is not None:
            raise ClientMetadataError(failure)
        task = self._in_flight.get(client_id)
        if task is None:
            task = asyncio.create_task(self._load(client_id))
            self._in_flight[client_id] = task
        # Shielded: a caller that disconnects mustn't cancel the shared fetch.
        return await asyncio.shield(task)

    async def _load(self, client_id: str) -> ClientMetadata:
        try:
            document = await fetch_client_metadata(
                client_id, allow_insecure_localhost=self._allow_insecure_localhost
            )
            metadata = _checked(client_id, document)
        except ClientMetadataError as exc:
            stale = self._documents.get(client_id, include_expired=True)
            if stale is None or self._clock() >= stale.stale_until:
                self._failures.put(client_id, str(exc), FAILURE_TTL_SECONDS)
                raise
            logger.warning(
                "serving stale OAuth client metadata for %r: refetch failed (%s)",
                client_id,
                type(exc.__cause__ or exc).__name__,
            )
            # Never past stale_until, even when the last retry falls just before it.
            ttl = min(FAILURE_TTL_SECONDS, stale.stale_until - self._clock())
            self._documents.put(client_id, stale, ttl)
            return stale.metadata
        else:
            stale_until = self._clock() + DOCUMENT_TTL_SECONDS + MAX_STALE_SECONDS
            self._documents.put(
                client_id, _Document(metadata, stale_until), DOCUMENT_TTL_SECONDS
            )
            return metadata
        finally:
            del self._in_flight[client_id]


def _checked(client_id: str, document: dict[str, Any]) -> ClientMetadata:
    if document.get("client_id") != client_id:
        raise ClientMetadataError(
            "client metadata document's client_id does not match its URL"
        )
    # A metadata document is public, so its client can hold no secret: the
    # token endpoint can only authenticate it as "none" (with PKCE). Any other
    # method would pass /authorize and consent, then fail every /token call.
    if document.get("token_endpoint_auth_method", "none") not in ("none", None):
        raise ClientMetadataError(
            "client metadata document's token_endpoint_auth_method must be none"
        )
    if document.get("client_secret") is not None:
        raise ClientMetadataError("client metadata document carries a client_secret")
    try:
        metadata = ClientMetadata.model_validate(
            {**document, "token_endpoint_auth_method": "none"}
        )
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        raise ClientMetadataError(
            f"client metadata document has invalid fields: {', '.join(fields)}"
        ) from exc
    # The SDK's type lets redirect_uris be null; a client with none can't log in.
    if not metadata.redirect_uris:
        raise ClientMetadataError("client metadata document lists no redirect_uris")
    return metadata


def _redirect_matches(requested: AnyUrl, registered: AnyUrl) -> bool:
    if requested == registered:
        return True
    loopback = _loopback_without_port(requested)
    return loopback is not None and loopback == _loopback_without_port(registered)


def _loopback_without_port(url: AnyUrl) -> tuple[str | None, ...] | None:
    """Every part of an ``http`` loopback URL but its port; None for any other URL.

    A URL with a user or password is not treated as loopback, so it only
    matches exactly.
    """
    if (
        url.scheme != "http"
        or (url.host or "").strip("[]") not in LOOPBACK_HOSTS
        or url.username is not None
        or url.password is not None
    ):
        return None
    return (url.host, url.path, url.query, url.fragment)


class _ExpiringLru[V]:
    """A dict capped at ``MAX_ENTRIES`` whose entries expire ``ttl`` seconds after they're put.

    An expired entry stays until evicted, so a caller can still ask for it.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, V]] = OrderedDict()

    def get(self, key: str, *, include_expired: bool = False) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at and not include_expired:
            return None
        self._entries.move_to_end(key)
        return value

    def put(self, key: str, value: V, ttl: float) -> None:
        self._entries[key] = (self._clock() + ttl, value)
        self._entries.move_to_end(key)
        if len(self._entries) > MAX_ENTRIES:
            self._entries.popitem(last=False)
