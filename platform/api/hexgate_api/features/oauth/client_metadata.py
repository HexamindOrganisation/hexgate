"""Fetch an OAuth client's metadata document without opening an SSRF hole.

An MCP client names itself by a URL, its Client ID Metadata Document, and the
API fetches that URL to learn where the client may redirect a login. Whoever
starts a login picks the URL, and the API sits on a private network, so an
unchecked fetch lets anyone make it call internal services. The guard resolves
the host once, refuses unless every address is public, and connects to the
address it checked: resolving again at connect time would let a DNS answer
that changed in between (rebinding) reach an address nobody checked.

The fetch follows no redirect (the target would skip the guard), stops after
``FETCH_TIMEOUT_SECONDS`` in total and reads at most ``MAX_DOCUMENT_BYTES``.
Caching the documents and checking what they say is the caller's job.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import SplitResult, urlsplit

import httpx

FETCH_TIMEOUT_SECONDS = 3.0
MAX_DOCUMENT_BYTES = 16 * 1024
# For every address but the last, so an unreachable first answer (an AAAA
# record seen from a container with no IPv6 route) leaves time for the next.
# httpcore applies it to the TCP connect and the TLS handshake separately, so
# a capped address can take up to twice this. The last address gets no cap of
# its own: a slow handshake on a host's only address may use the whole
# FETCH_TIMEOUT_SECONDS.
_CONNECT_TIMEOUT_SECONDS = 1.0

# Hosts an http client_id may name when insecure client ids are allowed
# (dev and tests only, so a client on the developer's machine can log in).
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# getaddrinfo can hold its thread long past FETCH_TIMEOUT_SECONDS. A pool of
# its own keeps a stuck resolver from starving asyncio.to_thread elsewhere in
# the API, as ai_act/render/pdf.py does for renders.
_dns_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="oauth-dns")

# Only global unicast IPv6 is fetched; that leaves out the IPv4-compatible,
# site-local and other legacy ranges ``is_global`` still calls global.
_GLOBAL_UNICAST_V6 = ipaddress.IPv6Network("2000::/3")
# RFC 6052 NAT64: on a NAT64 network this prefix reaches the IPv4 address in
# its last 32 bits, so that address is the one to check.
_NAT64_V6 = ipaddress.IPv6Network("64:ff9b::/96")

# Built once: a new context parses the whole CA bundle, a few ms of CPU on
# the event loop per fetch. Sharing it is safe because the hostname is checked
# per connection and the one setting httpcore writes to it per connection,
# the ALPN list, is always ["http/1.1"] here (no client enables http2).
_SSL_CONTEXT = httpx.create_ssl_context()

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class ClientMetadataError(Exception):
    """The client's metadata document could not be fetched or is not a JSON object.

    The message says why and names no internal address, so the caller may
    show it to the user who started the login.
    """


async def fetch_client_metadata(
    client_id: str, *, allow_insecure_localhost: bool = False
) -> dict[str, Any]:
    """Fetch the document at ``client_id`` and return its JSON object.

    ``client_id`` must be an ``https`` URL whose host resolves only to public
    addresses. With ``allow_insecure_localhost`` an ``http`` URL on a loopback
    host is accepted as well (``HEXGATE_OAUTH_ALLOW_INSECURE_CLIENT_IDS``).

    Raises ``ClientMetadataError`` for a refused URL, an unresolvable or
    non-public host, a timeout, a connection or TLS failure, any status but
    200 (redirects included), a body over the cap, and a body that is not a
    JSON object.
    """
    url = _parse_client_id(client_id, allow_insecure_localhost)
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            addresses = await _checked_addresses(
                url, allow_loopback=url.scheme == "http"
            )
            body = await _get_from_any(url, addresses)
    except TimeoutError as exc:
        raise ClientMetadataError(
            f"client metadata fetch timed out after {FETCH_TIMEOUT_SECONDS:g}s"
        ) from exc
    return _parse_document(body)


def _parse_client_id(client_id: str, allow_insecure_localhost: bool) -> SplitResult:
    # A URI is ASCII (RFC 3986); an IDN host arrives in its xn-- form. Refusing
    # the rest up front keeps raw Unicode out of the Host header and the SNI,
    # which httpx encodes as ASCII.
    if not client_id.isascii():
        raise ClientMetadataError("client_id must be an ASCII URL")
    try:
        url = urlsplit(client_id)
        url.port  # raises on a port that is not a number in range
        httpx.URL(client_id)  # refuses control characters urlsplit keeps
    except (ValueError, httpx.InvalidURL) as exc:
        raise ClientMetadataError("client_id is not a valid URL") from exc
    if not url.hostname:
        raise ClientMetadataError("client_id must be an absolute URL")
    if url.scheme == "https" or (
        allow_insecure_localhost
        and url.scheme == "http"
        and url.hostname in _LOOPBACK_HOSTS
    ):
        return url
    raise ClientMetadataError("client_id must be an https URL")


async def _checked_addresses(
    url: SplitResult, *, allow_loopback: bool
) -> list[IPAddress]:
    """Resolve the host and return its addresses, if every one is allowed.

    All of them are checked, not only the one that answers: a host resolving
    to one public and one private address is refused, since which of them
    gets connected to is not something to rely on.
    """
    addresses = await _resolve(url.hostname)
    if not addresses:
        raise ClientMetadataError(f"client_id host {url.hostname!r} did not resolve")
    for address in addresses:
        if not (_is_public(address) or (allow_loopback and address.is_loopback)):
            raise ClientMetadataError(
                f"client_id host {url.hostname!r} resolves to a non-public address"
            )
    return addresses


async def _resolve(host: str) -> list[IPAddress]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(
            _dns_pool, socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM
        )
    # UnicodeError: the idna codec refuses an empty or over-long label before
    # any lookup.
    except (socket.gaierror, UnicodeError) as exc:
        raise ClientMetadataError(f"client_id host {host!r} did not resolve") from exc
    # A link-local answer keeps its zone ("fe80::1%eth0"); ip_address parses
    # it and still classifies the address as link-local.
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def _is_public(address: IPAddress) -> bool:
    embedded = _embedded_ipv4(address)
    if embedded is not None:
        return _is_public(embedded)
    if address.version == 6 and address not in _GLOBAL_UNICAST_V6:
        return False
    return address.is_global and not address.is_multicast


def _embedded_ipv4(address: IPAddress) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address actually reaches, when it carries one.

    ``::ffff:10.0.0.5`` is 10.0.0.5 to the socket layer, and the 6to4, Teredo
    and NAT64 forms are routed to theirs, so the IPv4 address is what the
    guard must judge.
    """
    if address.version == 4:
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    if address in _NAT64_V6:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


async def _get_from_any(url: SplitResult, addresses: list[IPAddress]) -> bytes:
    """GET ``url`` from the first address that accepts a connection.

    Only a failure to connect moves on to the next address, and httpcore
    reports a failed TLS handshake as one; once an address answers over a
    verified connection, its answer is final.
    """
    for index, address in enumerate(addresses):
        last = index == len(addresses) - 1
        try:
            return await _get(url, address, None if last else _CONNECT_TIMEOUT_SECONDS)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            continue
        # ssl.SSLError: a TLS alert after the handshake (a server demanding a
        # client certificate under TLS 1.3) reaches here unmapped by httpcore.
        except (httpx.HTTPError, ssl.SSLError) as exc:
            raise ClientMetadataError(
                f"client metadata fetch failed: {type(exc).__name__}"
            ) from exc
    raise ClientMetadataError(
        f"could not connect to client_id host {url.hostname!r} or verify its "
        "certificate"
    )


async def _get(
    url: SplitResult, address: IPAddress, connect_timeout: float | None
) -> bytes:
    """GET ``url`` from ``address``, keeping the URL's host for Host and TLS.

    The request goes to the checked IP, never to the hostname, so nothing
    resolves it a second time. The certificate is still verified against the
    hostname, through the ``sni_hostname`` extension.
    """
    target = httpx.URL(
        scheme=url.scheme,
        host=str(address),
        port=url.port,
        path=url.path or "/",
        query=url.query.encode() or None,  # b"" would send a bare "?"
    )
    host_header = url.netloc.rpartition("@")[2]  # keeps [::1] brackets and the port
    # An explicit transport also means httpx applies no proxy from the
    # environment, which would connect (and resolve) where the guard never looked.
    async with httpx.AsyncClient(
        transport=_transport(),
        follow_redirects=False,
        timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS, connect=connect_timeout),
    ) as client:
        async with client.stream(
            "GET",
            target,
            headers={
                "Host": host_header,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            extensions={"sni_hostname": url.hostname},
        ) as response:
            if response.is_redirect:
                raise ClientMetadataError(
                    "client_id answered with a redirect, which is not followed"
                )
            if response.status_code != 200:
                raise ClientMetadataError(
                    f"client_id answered HTTP {response.status_code}"
                )
            return await _read_capped(response)


def _transport() -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport(verify=_SSL_CONTEXT)


async def _read_capped(response: httpx.Response) -> bytes:
    """Read the raw body, refusing it as soon as it passes the cap.

    Raw bytes, not decoded ones: the request asks for no compression, so a
    compressed answer fails to parse rather than inflating past the cap.
    """
    too_large = ClientMetadataError(
        f"client metadata document exceeds {MAX_DOCUMENT_BYTES} bytes"
    )
    declared = response.headers.get("Content-Length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > MAX_DOCUMENT_BYTES
    ):
        raise too_large
    body = bytearray()
    async for chunk in response.aiter_raw():
        body += chunk
        if len(body) > MAX_DOCUMENT_BYTES:
            raise too_large
    return bytes(body)


def _parse_document(body: bytes) -> dict[str, Any]:
    try:
        document = json.loads(body)
    # ValueError covers JSONDecodeError and UnicodeDecodeError; a deeply nested
    # array fits in the cap and overflows the parser's recursion instead.
    except (ValueError, RecursionError) as exc:
        raise ClientMetadataError("client metadata document is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ClientMetadataError("client metadata document is not a JSON object")
    return document
