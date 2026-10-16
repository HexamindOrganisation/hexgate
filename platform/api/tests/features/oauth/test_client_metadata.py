"""The client metadata fetch: what it returns, and every way it refuses.

DNS is faked through ``_resolve`` and the network through ``_transport``, so
each test states the addresses a host resolves to and what the server says.
"""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import socket
import ssl
import threading

import httpx
import pytest

from hexgate_api.features.oauth import client_metadata
from hexgate_api.features.oauth.client_metadata import (
    MAX_DOCUMENT_BYTES,
    ClientMetadataError,
    fetch_client_metadata,
)

CLIENT_ID = "https://client.example/oauth/metadata.json"
PUBLIC_IP = "93.184.216.34"
DOCUMENT = {"client_id": CLIENT_ID, "redirect_uris": ["http://localhost/cb"]}


@pytest.fixture
def resolves_to(monkeypatch):
    """Make every host resolve to the given addresses; returns the lookup log."""
    lookups: list[str] = []

    def install(*addresses: str) -> list[str]:
        async def fake_resolve(host):
            lookups.append(host)
            return [ipaddress.ip_address(a) for a in addresses]

        monkeypatch.setattr(client_metadata, "_resolve", fake_resolve)
        return lookups

    return install


@pytest.fixture
def server(monkeypatch):
    """Serve each request with ``handler``; returns the list of requests seen."""
    seen: list[httpx.Request] = []

    def install(handler) -> list[httpx.Request]:
        async def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return await handler(request)

        monkeypatch.setattr(
            client_metadata, "_transport", lambda: httpx.MockTransport(record)
        )
        return seen

    return install


def _body(content: bytes) -> httpx.Response:
    """A 200 whose body arrives as a stream, as from a real connection.

    A response built from bytes counts as already read, and the fetch reads
    the raw stream, so every test body goes through this.
    """

    async def stream():
        yield content

    return httpx.Response(200, content=stream())


def _serving(content: bytes):
    async def handler(request: httpx.Request) -> httpx.Response:
        return _body(content)

    return handler


_document = _serving(json.dumps(DOCUMENT).encode())


async def test_fetch_client_metadata_happy_path(resolves_to, server):
    resolves_to(PUBLIC_IP)
    seen = server(_document)

    assert await fetch_client_metadata(CLIENT_ID) == DOCUMENT
    assert seen[0].url.raw_path == b"/oauth/metadata.json"  # no stray "?"


async def test_when_fetching_then_connects_to_the_checked_address(resolves_to, server):
    # DNS rebinding: a second lookup at connect time could return 10.0.0.5.
    # The request must go to the address the guard checked, resolved once,
    # with the hostname kept for the Host header and the TLS certificate.
    lookups = resolves_to(PUBLIC_IP)
    seen = server(_document)

    await fetch_client_metadata("https://client.example:8443/meta?v=1")

    assert lookups == ["client.example"]
    request = seen[0]
    assert request.url.host == PUBLIC_IP
    assert request.url.port == 8443
    assert request.url.raw_path == b"/meta?v=1"
    assert request.headers["Host"] == "client.example:8443"
    assert request.extensions["sni_hostname"] == "client.example"
    assert request.headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize(
    "addresses",
    [
        ("10.0.0.5",),
        ("127.0.0.1",),
        ("169.254.169.254",),  # cloud metadata endpoint
        ("100.64.0.1",),  # carrier-grade NAT
        ("0.0.0.0",),
        ("224.0.0.1",),  # multicast, which is_global calls global
        ("::1",),
        ("fe80::1",),
        ("fc00::1",),
        ("fec0::1",),  # site-local, which is_global calls global
        ("::10.0.0.5",),  # IPv4-compatible, which is_global calls global
        ("::ffff:10.0.0.5",),  # IPv4-mapped
        ("2002:a00:5::1",),  # 6to4 of 10.0.0.5
        ("2001:0:4136:e378:8000:63bf:f5ff:fffa",),  # Teredo to 10.0.0.5
        ("64:ff9b::a00:5",),  # NAT64 of 10.0.0.5, which is_global calls global
        (PUBLIC_IP, "10.0.0.5"),  # one private answer among public ones
    ],
)
async def test_when_host_resolves_to_non_public_address_then_refused(
    resolves_to, server, addresses
):
    resolves_to(*addresses)
    seen = server(_document)

    with pytest.raises(ClientMetadataError, match="non-public address"):
        await fetch_client_metadata(CLIENT_ID)
    assert seen == []


@pytest.mark.parametrize(
    "address",
    [
        "::ffff:93.184.216.34",
        "64:ff9b::5db8:d822",  # NAT64
        "2002:5db8:d822::1",  # 6to4, which is_global calls non-global
        "2001:0:4136:e378:8000:63bf:a247:2dcb",  # Teredo to 93.184.210.52
    ],
)
async def test_when_ipv6_carries_public_ipv4_then_fetched(resolves_to, server, address):
    resolves_to(address)
    server(_document)

    assert await fetch_client_metadata(CLIENT_ID) == DOCUMENT


async def test_when_host_does_not_resolve_then_refused(resolves_to):
    resolves_to()

    with pytest.raises(ClientMetadataError, match="did not resolve"):
        await fetch_client_metadata(CLIENT_ID)


@pytest.mark.parametrize(
    ("client_id", "reason"),
    [
        # The idna codec refuses an empty label before any lookup.
        ("https://a..b/meta", "did not resolve"),
        # An IP literal resolves without the network.
        ("https://10.0.0.5/meta", "non-public address"),
        ("https://[fe80::1]/meta", "non-public address"),  # IPv6 through it too
    ],
)
async def test_when_real_resolver_refuses_then_refused(client_id, reason):
    # Neither case touches the network, so the outcome is the same offline.
    with pytest.raises(ClientMetadataError, match=reason):
        await fetch_client_metadata(client_id)


async def test_when_transport_is_real_then_private_host_refused_before_connecting(
    resolves_to,
):
    # The production transport, unpatched: the guard runs before it is used.
    resolves_to("10.0.0.5")

    with pytest.raises(ClientMetadataError, match="non-public address"):
        await fetch_client_metadata(CLIENT_ID)


@pytest.mark.parametrize(
    ("status", "headers", "reason"),
    [
        (302, {"Location": "http://10.0.0.5/"}, "redirect"),
        (404, {}, "HTTP 404"),
    ],
)
async def test_when_server_answers_other_than_200_then_refused(
    resolves_to, server, status, headers, reason
):
    async def answer(request):
        return httpx.Response(status, headers=headers)

    resolves_to(PUBLIC_IP)
    seen = server(answer)

    with pytest.raises(ClientMetadataError, match=reason):
        await fetch_client_metadata(CLIENT_ID)
    assert len(seen) == 1  # a redirect is not followed


async def test_when_body_streams_past_cap_without_length_then_refused(
    resolves_to, server
):
    chunks_sent = 0

    async def endless():
        nonlocal chunks_sent
        while True:
            chunks_sent += 1
            yield b" " * 1024

    async def stream(request):
        return httpx.Response(200, content=endless())

    resolves_to(PUBLIC_IP)
    server(stream)

    with pytest.raises(ClientMetadataError, match="exceeds"):
        await fetch_client_metadata(CLIENT_ID)
    assert chunks_sent == MAX_DOCUMENT_BYTES // 1024 + 1


@pytest.mark.parametrize("declare_length", [False, True])
async def test_when_body_is_exactly_the_cap_then_fetched(
    resolves_to, server, declare_length
):
    padding = MAX_DOCUMENT_BYTES - len(json.dumps({"pad": ""}))
    body = json.dumps({"pad": "x" * padding}).encode()
    assert len(body) == MAX_DOCUMENT_BYTES
    headers = {"Content-Length": str(len(body))} if declare_length else {}

    async def exact(request):
        async def stream():
            yield body

        return httpx.Response(200, headers=headers, content=stream())

    resolves_to(PUBLIC_IP)
    server(exact)

    assert await fetch_client_metadata(CLIENT_ID) == {"pad": "x" * padding}


async def test_when_server_is_slow_then_refused_at_the_total_timeout(
    resolves_to, server, monkeypatch
):
    monkeypatch.setattr(client_metadata, "FETCH_TIMEOUT_SECONDS", 0.05)

    async def slow(request):
        await asyncio.sleep(5)
        return await _document(request)

    resolves_to(PUBLIC_IP)
    server(slow)

    with pytest.raises(ClientMetadataError, match="timed out"):
        await fetch_client_metadata(CLIENT_ID)


async def test_when_resolver_hangs_then_refused_at_the_total_timeout(
    monkeypatch,
):
    monkeypatch.setattr(client_metadata, "FETCH_TIMEOUT_SECONDS", 0.05)

    async def hang(host):
        await asyncio.sleep(5)

    monkeypatch.setattr(client_metadata, "_resolve", hang)

    with pytest.raises(ClientMetadataError, match="timed out"):
        await fetch_client_metadata(CLIENT_ID)


async def test_when_no_address_accepts_a_connection_then_refused(resolves_to, server):
    async def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    resolves_to(PUBLIC_IP, "2606:4700::1")
    seen = server(refuse)

    with pytest.raises(ClientMetadataError, match="could not connect"):
        await fetch_client_metadata(CLIENT_ID)
    assert len(seen) == 2


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_when_first_address_is_unreachable_then_next_one_tried(
    resolves_to, server, failure
):
    # An AAAA answer seen from a container with no IPv6 route must not fail a
    # login the A record would serve.
    async def v6_unreachable(request):
        if request.url.host == "2606:4700::1":
            raise failure("unreachable", request=request)
        return await _document(request)

    resolves_to("2606:4700::1", PUBLIC_IP)
    seen = server(v6_unreachable)

    assert await fetch_client_metadata(CLIENT_ID) == DOCUMENT
    assert [r.url.host for r in seen] == ["2606:4700::1", PUBLIC_IP]


@pytest.mark.parametrize(
    ("addresses", "connect_timeouts"),
    [
        ((PUBLIC_IP,), [None]),
        (("2606:4700::1", PUBLIC_IP), [1.0, None]),
    ],
)
async def test_when_one_address_is_left_then_its_connect_is_not_capped(
    resolves_to, server, addresses, connect_timeouts
):
    # httpcore applies the connect timeout to TCP and to the TLS handshake
    # separately; a host's only or last address must get the whole budget.
    async def first_unreachable(request):
        if request.url.host == "2606:4700::1":
            raise httpx.ConnectError("unreachable", request=request)
        return await _document(request)

    resolves_to(*addresses)
    seen = server(first_unreachable)

    await fetch_client_metadata(CLIENT_ID)

    assert [r.extensions["timeout"]["connect"] for r in seen] == connect_timeouts


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadError("reset mid-body"),
        # A TLS alert after the handshake, which httpcore leaves unmapped.
        ssl.SSLError(1, "TLSV13_ALERT_CERTIFICATE_REQUIRED"),
    ],
)
async def test_when_first_address_answers_with_an_error_then_next_not_tried(
    resolves_to, server, failure
):
    async def broken(request):
        raise failure

    resolves_to(PUBLIC_IP, "2606:4700::1")
    seen = server(broken)

    with pytest.raises(ClientMetadataError, match=type(failure).__name__):
        await fetch_client_metadata(CLIENT_ID)
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b" " * (20 * 1024), "exceeds"),
        (b" " * (MAX_DOCUMENT_BYTES + 1), "exceeds"),
        (b"<html>not json</html>", "not valid JSON"),
        (b"\xff\xfe", "not valid JSON"),
        (b'["a list"]', "not a JSON object"),
        (b"[" * 16_000, "not valid JSON"),  # nesting past the parser's recursion
    ],
)
async def test_when_body_is_unacceptable_then_refused(
    resolves_to, server, body, reason
):
    resolves_to(PUBLIC_IP)
    server(_serving(body))

    with pytest.raises(ClientMetadataError, match=reason):
        await fetch_client_metadata(CLIENT_ID)


@pytest.mark.parametrize(
    ("client_id", "allow_insecure_localhost", "reason"),
    [
        ("http://client.example/meta", False, "https"),
        ("http://localhost:3000/meta", False, "https"),
        ("ftp://client.example/meta", False, "https"),
        ("/relative/meta", False, "absolute"),
        ("https://client.example:99999/meta", False, "not a valid URL"),
        ("https://[::1/meta", False, "not a valid URL"),
        ("https://client.example/meta\x7f", False, "not a valid URL"),
        ("https://bücher.example/meta", False, "ASCII"),  # xn-- form expected
        ("https://client.example/m?q=é", False, "ASCII"),
        # The flag opens http on loopback hosts only.
        ("http://client.example/meta", True, "https"),
        ("ftp://localhost:3000/meta", True, "https"),
    ],
)
async def test_when_client_id_is_not_an_https_url_then_refused(
    resolves_to, client_id, allow_insecure_localhost, reason
):
    lookups = resolves_to(PUBLIC_IP)

    with pytest.raises(ClientMetadataError, match=reason):
        await fetch_client_metadata(
            client_id, allow_insecure_localhost=allow_insecure_localhost
        )
    assert lookups == []


@pytest.mark.parametrize(
    ("client_id", "address", "host_header"),
    [
        ("http://localhost:3000/meta", "127.0.0.1", "localhost:3000"),
        ("http://127.0.0.1:3000/meta", "127.0.0.1", "127.0.0.1:3000"),
        ("http://[::1]:3000/meta", "::1", "[::1]:3000"),
    ],
)
async def test_when_insecure_localhost_allowed_then_http_loopback_fetched(
    resolves_to, server, client_id, address, host_header
):
    resolves_to(address)
    seen = server(_document)

    document = await fetch_client_metadata(client_id, allow_insecure_localhost=True)

    assert document == DOCUMENT
    assert seen[0].url.scheme == "http"
    assert seen[0].headers["Host"] == host_header


@pytest.mark.parametrize(
    ("client_id", "host_header"),
    [
        ("https://user:pw@client.example/meta", "client.example"),
        ("https://[2606:4700::1]:8443/meta", "[2606:4700::1]:8443"),
    ],
)
async def test_when_netloc_has_userinfo_or_brackets_then_host_header_is_clean(
    resolves_to, server, client_id, host_header
):
    resolves_to("2606:4700::1")
    seen = server(_document)

    await fetch_client_metadata(client_id)

    assert seen[0].headers["Host"] == host_header
    assert "Authorization" not in seen[0].headers


@pytest.mark.parametrize(
    ("client_id", "address"),
    [
        # An https client_id keeps the full guard, so a public name pointed at
        # 127.0.0.1 is still refused.
        (CLIENT_ID, "127.0.0.1"),
        # "localhost" is a name like any other: if it resolved to a LAN
        # address, the loopback allowance must not reach it.
        ("http://localhost:3000/meta", "10.0.0.5"),
    ],
)
async def test_when_insecure_localhost_allowed_then_non_loopback_still_refused(
    resolves_to, client_id, address
):
    resolves_to(address)

    with pytest.raises(ClientMetadataError, match="non-public address"):
        await fetch_client_metadata(client_id, allow_insecure_localhost=True)


async def test_when_declared_length_exceeds_cap_then_refused_before_reading(
    resolves_to, server
):
    chunks_sent = 0

    async def stream():
        nonlocal chunks_sent
        chunks_sent += 1
        yield b"{}"

    async def declared(request):
        headers = {"Content-Length": str(MAX_DOCUMENT_BYTES + 1)}
        return httpx.Response(200, headers=headers, content=stream())

    resolves_to(PUBLIC_IP)
    server(declared)

    with pytest.raises(ClientMetadataError, match="exceeds"):
        await fetch_client_metadata(CLIENT_ID)
    assert chunks_sent == 0


async def test_when_served_over_a_real_socket_then_fetched():
    # The real resolver and transport end to end, against a loopback server:
    # the request reaches the pinned address with the original Host header.
    requests: list[bytes] = []

    async def handle(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        body = json.dumps(DOCUMENT).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body)
        )
        await writer.drain()
        writer.close()

    listener = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    async with listener:
        # 127.0.0.1, not localhost: what localhost resolves to varies by
        # machine, and the listener binds IPv4 only.
        document = await fetch_client_metadata(
            f"http://127.0.0.1:{port}/meta", allow_insecure_localhost=True
        )

    assert document == DOCUMENT
    assert f"Host: 127.0.0.1:{port}".encode() in requests[0]


async def test_when_server_compresses_despite_identity_then_refused(
    resolves_to, server
):
    # The cap counts bytes off the wire. A compressed body is not inflated,
    # so a small gzip that would expand past the cap never gets the chance.
    async def gzipped(request):
        async def stream():
            yield gzip.compress(json.dumps(DOCUMENT).encode())

        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=stream()
        )

    resolves_to(PUBLIC_IP)
    server(gzipped)

    with pytest.raises(ClientMetadataError, match="not valid JSON"):
        await fetch_client_metadata(CLIENT_ID)


async def test_when_resolving_then_lookup_runs_on_its_own_pool(monkeypatch):
    threads: list[str] = []

    def fake_getaddrinfo(*args):
        threads.append(threading.current_thread().name)
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ClientMetadataError, match="did not resolve"):
        await fetch_client_metadata(CLIENT_ID)
    assert threads[0].startswith("oauth-dns")
