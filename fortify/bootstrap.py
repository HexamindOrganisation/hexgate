"""Bootstrap helpers for fortify."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from fortify import audit
from fortify.config.settings import Settings

_log = logging.getLogger(__name__)
_DEFAULT_API_URL = "http://localhost:8000"
_atexit_registered = False


def bootstrap(env_file: str = ".env") -> Settings:
    """Load environment variables and return validated settings.

    Also configures the process-wide audit sink when FORTIFY_KEY is set
    (silent no-op otherwise — local-only runs work without it).
    """
    env_path = Path(__file__).parent.parent / env_file
    load_dotenv(env_path, override=True)
    _maybe_configure_audit()
    settings = Settings.from_env()
    settings.validate_required_keys()
    return settings


def _maybe_configure_audit() -> None:
    """Wire the audit sink + atexit drain hook when FORTIFY_KEY is present."""
    global _atexit_registered
    api_key = os.environ.get("FORTIFY_KEY")
    if not api_key:
        return
    base_url = os.environ.get("FORTIFY_API_URL", _DEFAULT_API_URL).rstrip("/")
    audit.configure(f"{base_url}/v1/audit/decisions", api_key)
    if not _atexit_registered:
        atexit.register(_atexit_audit_shutdown)
        _atexit_registered = True


def _atexit_audit_shutdown() -> None:
    """Best-effort audit drain at process exit. The main loop is closed by the
    time this runs, so in-flight POSTs may be cut short — but the httpx client
    still closes cleanly via asyncio.run on a fresh loop."""
    try:
        asyncio.run(audit.shutdown())
    except Exception as exc:  # noqa: BLE001
        _log.debug("audit shutdown at exit failed: %s", exc)
