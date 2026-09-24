"""Shared fakes for the Google ADK adapter tests.

``_FakeToolset`` lives here rather than in one test module because both the
adapter gate tests and the manifest skill-discovery tests build on it.

Hand-rolled rather than ADK's ``SkillToolset``, which only exists from ADK
1.25.0 while ``pyproject.toml`` pins ``google-adk>=1.14``.
"""

from __future__ import annotations

from typing import Any

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.auth.auth_tool import AuthConfig
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset


class _FakeToolset(BaseToolset):
    """Minimal BaseToolset whose tool list the test controls."""

    def __init__(
        self,
        tools: list[BaseTool],
        *,
        dynamic: bool = False,
        auth_config: AuthConfig | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.closed = False
        self.llm_requests: list[Any] = []
        self.tools = tools
        self._auth_config = auth_config
        if dynamic:
            # Mirror what SkillToolset does to itself. Without this the INNER
            # memoizes per invocation and the cache tests pass no matter what
            # the wrapper does — a vacuous green.
            self._use_invocation_cache = False

    async def get_tools(
        self, readonly_context: ReadonlyContext | None = None
    ) -> list[BaseTool]:
        return list(self.tools)

    async def process_llm_request(self, *, tool_context: Any, llm_request: Any) -> None:
        self.llm_requests.append(llm_request)

    def get_auth_config(self) -> AuthConfig | None:
        return self._auth_config

    async def close(self) -> None:
        self.closed = True
