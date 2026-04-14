"""Bootstrap helpers for coolagents."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from coolagents.config.settings import Settings


def bootstrap(env_file: str = ".env") -> Settings:
    """Load environment variables and return validated settings."""
    env_path = Path(__file__).parent.parent / env_file
    load_dotenv(env_path, override=True)
    settings = Settings.from_env()
    settings.validate_required_keys()
    return settings
