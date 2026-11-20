"""Start-up configuration, read from the process environment only (prefix
``HEXGATE_ASSISTANT_``, except the issuer, which shares the API's
``HEXGATE_PUBLIC_URL``), never from the API's ``.env``."""

import os
from dataclasses import dataclass

from sqlalchemy import URL

# The port the server listens on, fixed here and not configurable.
PORT = 8090


@dataclass(frozen=True)
class Settings:
    # The assistant's own database. The main database's login is never given
    # to this service. A URL object, whose repr hides the password.
    database_url: URL
    # The API's public URL, the only accepted token `iss`. It agrees with the
    # API only when HEXGATE_PUBLIC_URL is exported to both processes.
    issuer: str = "http://localhost:8000"
    # The assistant's own resource URL, the only accepted token `aud`. Never
    # the MCP server's: every token an MCP client holds names that one.
    resource_url: str = f"http://localhost:{PORT}/v1/assistant"
    # Where the JWKS is fetched: the private network in deploy.
    api_url: str = "http://localhost:8000"
    # Loopback unless the environment says otherwise, so `make assistant-run`
    # is not reachable from the network. The image opts in to all interfaces.
    bind_host: str = "127.0.0.1"
    port: int = PORT

    @classmethod
    def from_env(cls) -> "Settings":
        issuer = os.environ.get("HEXGATE_PUBLIC_URL", cls.issuer)
        if issuer.endswith("/"):
            raise ValueError(
                f"public_url={issuer!r} ends with '/' — set "
                "HEXGATE_PUBLIC_URL without the trailing slash."
            )
        return cls(
            database_url=database_url_from_env(),
            issuer=issuer,
            resource_url=os.environ.get(
                "HEXGATE_ASSISTANT_RESOURCE_URL", cls.resource_url
            ),
            api_url=os.environ.get("HEXGATE_ASSISTANT_API_URL", cls.api_url),
            bind_host=os.environ.get("HEXGATE_ASSISTANT_BIND_HOST", cls.bind_host),
        )

    @property
    def jwks_url(self) -> str:
        return f"{self.api_url.rstrip('/')}/.well-known/jwks.json"


def database_url_from_env() -> URL:
    """The Postgres URL from the ``HEXGATE_ASSISTANT_POSTGRES_*`` parts. The
    password has no default: without it the server refuses to start rather
    than run with nowhere to keep its sessions."""
    password = os.environ.get("HEXGATE_ASSISTANT_POSTGRES_PASSWORD", "")
    if not password:
        raise ValueError(
            "HEXGATE_ASSISTANT_POSTGRES_PASSWORD is not set — the assistant "
            "needs its own database (hexgate_assistant)."
        )
    return URL.create(
        "postgresql+asyncpg",
        username=os.environ.get("HEXGATE_ASSISTANT_POSTGRES_USER", "hexgate_assistant"),
        password=password,
        host=os.environ.get("HEXGATE_ASSISTANT_POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("HEXGATE_ASSISTANT_POSTGRES_PORT", "5432")),
        database=os.environ.get("HEXGATE_ASSISTANT_POSTGRES_DB", "hexgate_assistant"),
    )
