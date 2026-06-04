"""Bootstrap helpers for fortify."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from fortify.config.settings import Settings


def bootstrap(env_file: str = ".env") -> Settings:
    """Load environment variables and return validated settings.

    ``override=False`` so a shell-set env var wins over the same key in
    ``.env`` — matches the convention every other tool (uvicorn, vite,
    cargo, npm…) follows. Treats ``.env`` as a default-provider, not
    an authoritative override.
    """
    env_path = Path(__file__).parent.parent / env_file
    load_dotenv(env_path, override=False)
    settings = Settings.from_env()
    settings.validate_required_keys()
    return settings
