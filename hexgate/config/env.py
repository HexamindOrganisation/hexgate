"""Resolution for the platform API credential env var.

``HEXGATE_KEY`` was renamed to ``HEXGATE_API_KEY`` — for symmetry with
``HEXGATE_API_URL`` and with the ``api_key`` parameter it feeds, and to
disambiguate it from the signing-key family (``HEXGATE_KEYSTORE_PATH``,
``HEXGATE_PUBLIC_KEY``). The old name is no longer read; a one-time warning
fires when it is set alone so the rename is obvious rather than silently
treated as "no key".
"""

from __future__ import annotations

import logging
import os

API_KEY_ENV = "HEXGATE_API_KEY"
LEGACY_API_KEY_ENV = "HEXGATE_KEY"
API_URL_ENV = "HEXGATE_API_URL"

# Defaults to Hexgate Cloud: the common case is a hosted key, so an unset
# URL should "just work" for it. Self-hosters / local platform runs set
# HEXGATE_API_URL=http://localhost:8000 explicitly. The key and URL are
# coupled — a key only verifies against the platform instance that minted it.
DEFAULT_API_URL = "https://app.hexgate.ai"

_log = logging.getLogger(__name__)
_warned_legacy = False


def resolve_api_key(explicit: str | None = None) -> str | None:
    """Resolve the platform API key: ``explicit`` arg → ``HEXGATE_API_KEY``.

    Returns ``None`` when neither is set. If the retired ``HEXGATE_KEY`` is
    set while ``HEXGATE_API_KEY`` is not, emit a one-time warning pointing at
    the rename — the legacy value is not used.
    """
    if explicit:
        return explicit
    key = os.environ.get(API_KEY_ENV)
    if key:
        return key
    if os.environ.get(LEGACY_API_KEY_ENV):
        global _warned_legacy
        if not _warned_legacy:
            _log.warning(
                "HEXGATE_KEY is set but no longer read; rename it to HEXGATE_API_KEY."
            )
            _warned_legacy = True
    return None
