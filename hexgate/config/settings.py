"""Runtime settings for hexgate."""

from __future__ import annotations

import os

from dataclasses import dataclass


@dataclass(slots=True)
class Settings:
    """Runtime settings loaded from environment."""

    openai_api_key: str | None
    linkup_api_key: str | None
    tavily_api_key: str | None
    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_host: str
    model: str
    search_engine: str

    @classmethod
    def from_env(cls) -> "Settings":
        """Create settings from process environment."""
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            linkup_api_key=os.getenv("LINKUP_API_KEY"),
            tavily_api_key=os.getenv("TAVILY_API_KEY"),
            langfuse_public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
            langfuse_secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
            langfuse_host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            model=os.getenv("HEXGATE_DEFAULT_MODEL", "openai:gpt-5.4"),
            search_engine=os.getenv("HEXGATE_DEFAULT_SEARCH_ENGINE", "linkup"),
        )
