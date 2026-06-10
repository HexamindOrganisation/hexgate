"""Bootstrap helpers for hexgate."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from hexgate import audit
from hexgate.config.settings import Settings


def bootstrap(env_file: str = ".env") -> Settings:
    """Load environment variables and return validated settings.

    ``override=False`` so a shell-set env var wins over the same key in
    ``.env`` — matches the convention every other tool (uvicorn, vite,
    cargo, npm…) follows. Treats ``.env`` as a default-provider, not
    an authoritative override.

    Also configures the process-wide audit sender (silent no-op when
    HEXGATE_KEY isn't set — local-only runs work without it). Audit
    sends are fire-and-forget background tasks: when the event loop
    tears down at exit they are cancelled, not finished, so events
    emitted shortly before exit are lost unless the teardown path
    explicitly drains with `await audit.shutdown()`.
    """
    env_path = Path(__file__).parent.parent / env_file
    # ``override=False``: shell wins over .env, matching the convention
    # uvicorn / vite / cargo / npm follow. A pre-existing test
    # (test_bootstrap_loads_requested_env_file) pinned this contract;
    # the code had drifted to ``True``. Flipped back here so CI is green.
    load_dotenv(env_path, override=False)
    audit.configure()
    settings = Settings.from_env()
    settings.validate_required_keys()
    return settings
