"""HTTP client for the Fortify control plane — stdlib only, no added deps."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 10.0
TOKEN_PREFIX = "fty_"
DEFAULT_AGENT_NAME = "default"


def resolve_agent_name(explicit: str | None = None) -> str:
    """Resolve the agent name for a Fortify call.

    Precedence: explicit arg → FORTIFY_AGENT_NAME env → "default".
    """
    if explicit:
        return explicit
    return os.environ.get("FORTIFY_AGENT_NAME") or DEFAULT_AGENT_NAME


class FortifyError(RuntimeError):
    """Raised for any Fortify API interaction failure."""


@dataclass
class FortifyConfig:
    """Resolved configuration for a Fortify client."""

    base_url: str
    api_key: str
    project_id: str

    @classmethod
    def from_env(
        cls,
        *,
        project_id: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> "FortifyConfig":
        """Resolve configuration from explicit args → env → key prefix."""
        key = api_key or os.environ.get("FORTIFY_KEY")
        if not key:
            raise FortifyError(
                "FORTIFY_KEY not set — export it or pass api_key= explicitly"
            )

        url = (base_url or os.environ.get("FORTIFY_API_URL") or DEFAULT_BASE_URL).rstrip("/")

        resolved_project = (
            project_id
            or os.environ.get("FORTIFY_PROJECT_ID")
            or _parse_project_from_key(key)
        )
        if not resolved_project:
            raise FortifyError(
                "Unable to resolve project id — set FORTIFY_PROJECT_ID, pass "
                "project_id=, or use a key that encodes the project "
                "(fty_<env>_<project>_<secret>)"
            )

        return cls(base_url=url, api_key=key, project_id=resolved_project)


def _parse_project_from_key(key: str) -> str | None:
    """Best-effort parse of ``fty_<env>_<project>_<secret>`` → project id."""
    if not key.startswith(TOKEN_PREFIX):
        return None
    # fty_test_support-bot_abc...  →  ["fty", "test", "support-bot", "abc..."]
    parts = key.split("_", 3)
    if len(parts) < 4:
        return None
    return parts[2] or None


class FortifyClient:
    """Minimal HTTP client scoped to a single project + key."""

    def __init__(self, config: FortifyConfig, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.config = config
        self.timeout = timeout

    @classmethod
    def from_env(cls, **kwargs: Any) -> "FortifyClient":
        return cls(FortifyConfig.from_env(**kwargs))

    def get_agent(self, name: str) -> dict[str, Any]:
        """Fetch {agent_yaml, policy_yaml, system_md, ...} for a named agent."""
        url = (
            f"{self.config.base_url}/v1/projects/"
            f"{self.config.project_id}/agents/{name}"
        )
        return self._get(url)

    def _get(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Accept": "application/json",
                "User-Agent": "coolagents-fortify/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise FortifyError(
                f"Fortify API error {exc.code} calling {url}: {detail[:200]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise FortifyError(f"Fortify API unreachable at {url}: {exc.reason}") from exc
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FortifyError(f"Fortify API returned non-JSON from {url}") from exc
