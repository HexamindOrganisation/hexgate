"""Environment configuration, read once at start-up (prefix ``HEXGATE_MCP_``)."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Where the server calls the API, and fetches its JWKS: the private network
    # in deploy, so neither request leaves it.
    api_url: str
    # The API's public URL, the only accepted token `iss`.
    issuer: str
    # The URL clients connect to, the only accepted token `aud`.
    resource_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_url=os.environ.get("HEXGATE_MCP_API_URL", "http://localhost:8000"),
            issuer=os.environ.get("HEXGATE_MCP_ISSUER", "http://localhost:8000"),
            resource_url=os.environ.get(
                "HEXGATE_MCP_RESOURCE_URL", "http://localhost:8080/mcp"
            ),
        )

    @property
    def jwks_url(self) -> str:
        return f"{self.api_url.rstrip('/')}/.well-known/jwks.json"
