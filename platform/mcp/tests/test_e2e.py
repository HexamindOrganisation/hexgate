"""End to end: an MCP client logs in through the API's OAuth server and saves a
policy through this server, with nothing stubbed.

The API runs as a subprocess from its own project (platform/api has its own
lock) on a throwaway SQLite file; this server runs in-process on a loopback
port; the client is the MCP SDK's own OAuth client, identified by a client-ID
metadata document served on a third loopback port. The only hand-driven step
is the consent page: the test POSTs what the dashboard would, with a cookie
session, ticking one of the user's two projects.

Opt-in: ``pytest -m integration``. Needs ``uv`` and, for the policy save,
``opa`` on PATH.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.types import CallToolResult, TextContent

from hexgate_mcp.scopes import OAUTH_SCOPES
from hexgate_mcp.server import create_app
from hexgate_mcp.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH"),
]
# Saving the entry file compiles the project's bundle with opa.
needs_opa = pytest.mark.skipif(
    shutil.which("opa") is None and "HEXGATE_OPA_BIN" not in os.environ,
    reason="opa not on PATH and HEXGATE_OPA_BIN unset",
)

API_PROJECT = Path(__file__).resolve().parents[2] / "api"
HOST = "127.0.0.1"
PASSWORD = "e2e-Password-1234"
POLICY = "tools: { a: { mode: allow } }\n"
CLIENT_NAME = "Hexgate e2e"


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


@dataclass(frozen=True)
class Stack:
    api_url: str
    mcp_url: str
    dashboard_url: str
    client_id: str
    redirect_uri: str


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    tmp = tmp_path_factory.mktemp("e2e")
    api_port, mcp_port, doc_port = free_port(), free_port(), free_port()
    s = Stack(
        api_url=f"http://{HOST}:{api_port}",
        mcp_url=f"http://{HOST}:{mcp_port}/mcp",
        # Never contacted: the consent POST must only carry its origin.
        dashboard_url=f"http://{HOST}:{free_port()}",
        client_id=f"http://{HOST}:{doc_port}/client.json",
        # Loopback redirects match on any port, as for a native client.
        redirect_uri=f"http://{HOST}:{free_port()}/callback",
    )
    with (
        _client_document(doc_port, s),
        _api(api_port, s, tmp),
        _mcp_server(mcp_port, s),
    ):
        yield s


@contextmanager
def _client_document(port: int, s: Stack) -> Iterator[None]:
    """The client-ID metadata document, as a client such as Claude Code hosts it."""
    body = json.dumps(
        {
            "client_id": s.client_id,
            "client_name": CLIENT_NAME,
            "redirect_uris": [f"http://{HOST}/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer((HOST, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _api(port: int, s: Stack, tmp: Path) -> Iterator[None]:
    # None of the shell's own HEXGATE_* settings but the opa binary: the API
    # sees only these. The API's own .env does not override them.
    env = {
        **{
            k: v
            for k, v in os.environ.items()
            if not k.startswith("HEXGATE_") or k == "HEXGATE_OPA_BIN"
        },
        # The API project runs from source, not as an installed package.
        "PYTHONPATH": str(API_PROJECT),
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp / 'hexgate.db'}",
        "HEXGATE_KEYSTORE_PATH": str(tmp / "keys"),
        "HEXGATE_SEED": "skip",
        # The session cookie must travel over plain http to 127.0.0.1.
        "HEXGATE_COOKIE_SECURE": "0",
        "HEXGATE_PUBLIC_URL": s.api_url,
        "HEXGATE_MCP_RESOURCE_URL": s.mcp_url,
        "HEXGATE_DASHBOARD_URL": s.dashboard_url,
        "HEXGATE_OAUTH_ALLOW_INSECURE_CLIENT_IDS": "1",
        # Nothing listens there: the audit endpoints are not on this path.
        "HEXGATE_CLICKHOUSE_HOST": HOST,
        "HEXGATE_CLICKHOUSE_PORT": str(free_port()),
    }
    command = [
        "uv", "run", "--project", str(API_PROJECT), "--frozen",
        "python", "-m", "uvicorn", "hexgate_api.main:app",
        "--host", HOST, "--port", str(port),
    ]  # fmt: skip
    log_path = tmp / "api.log"
    with log_path.open("w") as log:
        # Its own process group, so a kill reaches uvicorn and not only uv.
        process = subprocess.Popen(
            command,
            cwd=tmp,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            _wait_for(f"{s.api_url}/health", process, log_path)
            yield
        finally:
            _kill_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill_group(process, signal.SIGKILL)
                process.wait()


def _kill_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    # Gone already when the API exited on its own (a failed start).
    with suppress(ProcessLookupError):
        os.killpg(process.pid, sig)


def _wait_for(url: str, process: subprocess.Popen[bytes], log: Path) -> None:
    # The first run syncs the API's environment, hence the long wait.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"the API exited:\n{log.read_text()}")
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    pytest.fail(f"the API did not start:\n{log.read_text()}")


@contextmanager
def _mcp_server(port: int, s: Stack) -> Iterator[None]:
    settings = Settings(api_url=s.api_url, issuer=s.api_url, resource_url=s.mcp_url)
    config = uvicorn.Config(
        create_app(settings), host=HOST, port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "the MCP server did not start"
            time.sleep(0.05)
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class Account:
    """A dashboard user: their cookie session on the API."""

    def __init__(self, s: Stack, email: str) -> None:
        self.http = httpx.Client(base_url=s.api_url)
        register = self.http.post(
            "/v1/auth/register", json={"email": email, "password": PASSWORD}
        )
        assert register.status_code == 201, register.text
        login = self.http.post(
            "/v1/auth/cookie/login", data={"username": email, "password": PASSWORD}
        )
        assert login.status_code == 204, login.text

    def ok(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.http.request(method, path, **kwargs)
        assert response.is_success, f"{method} {path}: {response.text}"
        return response.json()


@dataclass(frozen=True)
class Org:
    admin: Account
    member: Account
    ticked: str
    unticked: str


@pytest.fixture(scope="module")
def org(stack: Stack) -> Org:
    """An admin with two projects in their org, and a member of that org."""
    admin = Account(stack, "admin@example.com")
    member = Account(stack, "member@example.com")
    (home,) = admin.ok("GET", "/v1/orgs")
    projects = f"/v1/orgs/{home['id']}/projects"
    ticked = admin.ok("POST", projects, json={"name": "ticked"})["id"]
    unticked = admin.ok("POST", projects, json={"name": "unticked"})["id"]
    invite = admin.ok(
        "POST",
        f"/v1/orgs/{home['id']}/invites",
        json={"email": "member@example.com", "role": "member"},
    )
    member.ok("POST", f"/v1/invites/{invite['id']}/accept")
    return Org(admin=admin, member=member, ticked=ticked, unticked=unticked)


class MemoryStorage:
    """The SDK's token storage, in memory: one login per test."""

    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


class Consent:
    """The browser half of the login: the user, already signed in to the
    dashboard, approves the request for the projects they tick."""

    def __init__(self, s: Stack, user: Account, projects: list[str]) -> None:
        self.s, self.user, self.projects = s, user, projects
        self.callback: tuple[str, str | None] | None = None

    async def redirect(self, authorization_url: str) -> None:
        # /authorize checks the request and hands it to the consent page.
        sent = self.user.http.get(authorization_url)
        assert sent.status_code == 302, sent.text
        page = urlsplit(sent.headers["location"])
        assert f"{page.scheme}://{page.netloc}{page.path}" == (
            f"{self.s.dashboard_url}/oauth/consent"
        )
        request = {k: v for k, (v,) in parse_qs(page.query).items()}
        # What the consent page loads to show the request and the projects.
        info = self.user.ok("GET", "/v1/oauth/consent-info", params=request)
        assert info["client_name"] == CLIENT_NAME
        assert set(self.projects) <= {p["id"] for p in info["projects"]}
        # What it POSTs on "Approve".
        approved = self.user.ok(
            "POST",
            "/v1/oauth/authorize",
            json={**request, "projects": self.projects},
            headers={"Origin": self.s.dashboard_url},
        )
        back = parse_qs(urlsplit(approved["redirect_to"]).query)
        self.callback = (back["code"][0], back.get("state", [None])[0])

    async def callback_handler(self) -> tuple[str, str | None]:
        assert self.callback is not None, "the redirect never ran"
        return self.callback


def oauth_client(
    s: Stack, consent: Consent, storage: MemoryStorage
) -> OAuthClientProvider:
    provider = OAuthClientProvider(
        server_url=s.mcp_url,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[s.redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=consent.redirect,
        callback_handler=consent.callback_handler,
    )
    # The SDK takes a client-ID metadata URL only over https, and the API
    # fetches an http one only from loopback (both by design), so the test's
    # loopback URL is set after the constructor's check. From here on the
    # SDK's own discovery chooses it over registration because the API's
    # metadata advertises client_id_metadata_document_supported.
    provider.context.client_metadata_url = s.client_id
    return provider


@asynccontextmanager
async def mcp_session(
    s: Stack, auth: httpx.Auth | None = None, token: str | None = None
) -> AsyncIterator[ClientSession]:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with (
        httpx.AsyncClient(auth=auth, headers=headers, timeout=30) as http,
        streamable_http_client(s.mcp_url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def text(result: CallToolResult) -> str:
    (content,) = result.content
    assert isinstance(content, TextContent)
    return content.text


async def list_files(session: ClientSession, project_id: str) -> CallToolResult:
    return await session.call_tool("policy_list_files", {"project_id": project_id})


async def save(session: ClientSession, project_id: str) -> CallToolResult:
    arguments = {"project_id": project_id, "name": "policy.yaml", "content": POLICY}
    return await session.call_tool("policy_save", arguments)


@dataclass(frozen=True)
class Connection:
    session: ClientSession
    tokens: OAuthToken
    client_id: str


@asynccontextmanager
async def login(
    s: Stack, user: Account, projects: list[str]
) -> AsyncIterator[Connection]:
    """An MCP session logged in through the SDK as `user`, who consents to
    `projects`."""
    storage = MemoryStorage()
    provider = oauth_client(s, Consent(s, user, projects), storage)
    async with mcp_session(s, auth=provider) as session:
        # initialize is the first request: its 401 starts discovery and login.
        assert storage.tokens is not None
        assert storage.client_info is not None
        yield Connection(session, storage.tokens, storage.client_info.client_id)


@needs_opa
async def test_login_to_save_happy_path(stack: Stack, org: Org) -> None:
    async with login(stack, org.admin, [org.ticked]) as connection:
        assert connection.client_id == stack.client_id
        # The SDK asks for every scope the resource metadata lists.
        assert set(connection.tokens.scope.split()) == OAUTH_SCOPES

        listed = await list_files(connection.session, org.ticked)
        assert not listed.isError, text(listed)
        saved = await save(connection.session, org.ticked)
        assert not saved.isError, text(saved)
        assert json.loads(text(saved))["name"] == "policy.yaml"

        # Not ticked at consent, though the user is an admin there: the MCP
        # server refuses before any request...
        refused = await save(connection.session, org.unticked)
        assert refused.isError
        assert "not one of the projects granted" in text(refused)

    # ...and the API refuses the same token on its own.
    direct = httpx.put(
        f"{stack.api_url}/v1/projects/{org.unticked}/policy-files/policy.yaml",
        json={"content": POLICY},
        headers={"Authorization": f"Bearer {connection.tokens.access_token}"},
    )
    assert direct.status_code == 403
    assert direct.json()["detail"] == "token does not cover this project"


async def test_when_a_member_saves_then_the_api_refuses(stack: Stack, org: Org) -> None:
    async with login(stack, org.member, [org.ticked]) as connection:
        listed = await list_files(connection.session, org.ticked)
        assert not listed.isError, text(listed)
        saved = await save(connection.session, org.ticked)
    # The token carries policy:write: the API's project-admin check refuses.
    assert saved.isError
    assert "admin or owner role required" in text(saved)


@pytest.mark.skip(
    reason="needs the refresh_token grant and /v1/oauth/revoke, which land with "
    "the OAuth refresh branch (vl/feat/oauth_refresh); un-skip once it merges"
)
async def test_when_refreshed_then_revoked_then_the_next_refresh_fails(
    stack: Stack, org: Org
) -> None:
    async with login(stack, org.admin, [org.ticked]) as connection:
        first = connection.tokens

    def refresh(token: str | None) -> httpx.Response:
        return httpx.post(
            f"{stack.api_url}/v1/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": stack.client_id,
            },
        )

    refreshed = refresh(first.refresh_token)
    assert refreshed.status_code == 200, refreshed.text
    rotated = refreshed.json()
    assert rotated["refresh_token"] != first.refresh_token

    # The rotated access token still covers the ticked project.
    async with mcp_session(stack, token=rotated["access_token"]) as session:
        listed = await list_files(session, org.ticked)
    assert not listed.isError, text(listed)

    revoked = httpx.post(
        f"{stack.api_url}/v1/oauth/revoke",
        data={"token": rotated["refresh_token"], "client_id": stack.client_id},
    )
    assert revoked.status_code == 200
    again = refresh(rotated["refresh_token"])
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"
