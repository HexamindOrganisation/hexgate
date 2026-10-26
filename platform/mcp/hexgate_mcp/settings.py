"""Environment configuration, read once at start-up from the process
environment only (prefix ``HEXGATE_MCP_``, except the issuer, which shares the
API's ``HEXGATE_PUBLIC_URL``). The API's ``.env`` is never read here."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# The only HEXGATE_PUBLIC_URL hosts under which policy_write_local may run.
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
# Refused as the policy root itself, not what lies below them: temp folders
# live under /var (macOS) and /tmp, and a test or a scratch root may sit there.
SYSTEM_DIRS = ["/", "/etc", "/usr", "/var", "/opt", "/bin", "/sbin", "/lib", "/tmp"]


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
        return cls(
            api_url=os.environ.get("HEXGATE_MCP_API_URL", "http://localhost:8000"),
            issuer=issuer,
            resource_url=os.environ.get(
                "HEXGATE_MCP_RESOURCE_URL", "http://localhost:8080/mcp"
            ),
            local_policy_dir=read_local_policy_dir(issuer),
        )

    @property
    def bind_host(self) -> str:
        """Loopback only while the server can write to this machine's disk."""
        return "0.0.0.0" if self.local_policy_dir is None else "127.0.0.1"

    @property
    def api_v1_url(self) -> str:
        return f"{self.api_url.rstrip('/')}/v1"

    @property
    def jwks_url(self) -> str:
        return f"{self.api_url.rstrip('/')}/.well-known/jwks.json"


def read_local_policy_dir(issuer: str) -> Path | None:
    """HEXGATE_MCP_LOCAL_POLICY_DIR, once it is a policies folder on a local
    server: never a repo root, a system directory, or the home directory or
    one of its ancestors, where the tool's name rules alone would be all that
    kept a model from the rest."""
    raw_dir = os.environ.get("HEXGATE_MCP_LOCAL_POLICY_DIR")
    if not raw_dir:
        return None
    # First, so a stray variable in a deploy env, where the path usually does
    # not exist, is told why rather than to create it.
    if urlparse(issuer).hostname not in LOCAL_HOSTS:
        raise ValueError(
            "HEXGATE_MCP_LOCAL_POLICY_DIR is for local development only — "
            f"unset it, or point HEXGATE_PUBLIC_URL (now {issuer!r}) at localhost."
        )
    path = Path(raw_dir)
    if not path.is_dir():
        raise ValueError(
            f"local_policy_dir={raw_dir!r} is not a directory — "
            "create it, or unset HEXGATE_MCP_LOCAL_POLICY_DIR."
        )
    real = path.resolve()
    system = {Path(d).resolve() for d in SYSTEM_DIRS}
    if real in system or Path.home().resolve().is_relative_to(real):
        raise ValueError(
            f"local_policy_dir={raw_dir!r} is a system directory, or the home "
            "directory or above it — point HEXGATE_MCP_LOCAL_POLICY_DIR at a "
            "policies folder."
        )
    if (path / ".git").exists():
        raise ValueError(
            f"local_policy_dir={raw_dir!r} is a repo root — point "
            "HEXGATE_MCP_LOCAL_POLICY_DIR at a policies folder."
        )
    return path
