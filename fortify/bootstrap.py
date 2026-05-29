"""Bootstrap helpers for fortify."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from fortify import audit
from fortify.config.settings import Settings


def bootstrap(env_file: str = ".env") -> Settings:
    """Load environment variables and return validated settings.

    Also configures the process-wide audit sink (silent no-op when
    FORTIFY_KEY isn't set — local-only runs work without it). Callers
    that want graceful drain on shutdown should `await audit.shutdown()`
    explicitly in their teardown path; the httpx client is otherwise
    closed by Python's garbage collector at exit.
    """
    env_path = Path(__file__).parent.parent / env_file
    load_dotenv(env_path, override=True)
    audit.configure()
    settings = Settings.from_env()
    settings.validate_required_keys()
    return settings
