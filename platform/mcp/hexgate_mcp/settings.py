"""Environment configuration, read once at start-up from the process
environment only (prefix ``HEXGATE_MCP_``, except the issuer, which shares the
API's ``HEXGATE_PUBLIC_URL``). The API's ``.env`` is never read here."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # Where the server calls the API, and fetches its JWKS: the private network
    # in deploy, so neither request leaves it.
    api_url: str
    # The API's public URL, the only accepted token `iss`. It agrees with the
    # API only when HEXGATE_PUBLIC_URL is exported to both processes: this
    # server does not read the API's .env. In deploy both services get it from
    # the same compose env.
    issuer: str
    # The URL clients connect to, the only accepted token `aud`.
    resource_url: str
    # Where policy_write_local writes; None (the deploy value) leaves the
    # tool unregistered.
    local_policy_dir: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        issuer = os.environ.get("HEXGATE_PUBLIC_URL", "http://localhost:8000")
        if issuer.endswith("/"):
            raise ValueError(
                f"public_url={issuer!r} ends with '/' — set "
                "HEXGATE_PUBLIC_URL without the trailing slash."
            )
        local_policy_dir = None
        if raw_dir := os.environ.get("HEXGATE_MCP_LOCAL_POLICY_DIR"):
            local_policy_dir = Path(raw_dir)
            if not local_policy_dir.is_dir():
                raise ValueError(
                    f"local_policy_dir={raw_dir!r} is not a directory — "
                    "create it, or unset HEXGATE_MCP_LOCAL_POLICY_DIR."
                )
        return cls(
            api_url=os.environ.get("HEXGATE_MCP_API_URL", "http://localhost:8000"),
            issuer=issuer,
            resource_url=os.environ.get(
                "HEXGATE_MCP_RESOURCE_URL", "http://localhost:8080/mcp"
            ),
            local_policy_dir=local_policy_dir,
        )

    @property
    def api_v1_url(self) -> str:
        return f"{self.api_url.rstrip('/')}/v1"

    @property
    def jwks_url(self) -> str:
        return f"{self.api_url.rstrip('/')}/.well-known/jwks.json"
