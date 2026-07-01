"""Resolution for the platform API credential env var.

``HEXGATE_KEY`` was renamed to ``HEXGATE_API_KEY`` — for symmetry with
``HEXGATE_API_URL`` and with the ``api_key`` parameter it feeds, and to
disambiguate it from the signing-key family (``HEXGATE_KEYSTORE_PATH``,
``HEXGATE_PUBLIC_KEY``). The legacy name is still honored, with a one-time
warning, so existing ``.env`` files keep working; drop the fallback once
downstream configs have migrated.
"""

from __future__ import annotations

import logging
import os

API_KEY_ENV = "HEXGATE_API_KEY"
LEGACY_API_KEY_ENV = "HEXGATE_KEY"
API_URL_ENV = "HEXGATE_API_URL"

_log = logging.getLogger(__name__)
_warned_legacy = False


def resolve_api_key(explicit: str | None = None) -> str | None:
    """Resolve the platform API key.

    Precedence: ``explicit`` arg → ``HEXGATE_API_KEY`` → deprecated
    ``HEXGATE_KEY``. Returns ``None`` when none is set. Emits a one-time
    warning when the resolved value came from the deprecated env var.
    """
    if explicit:
        return explicit
    key = os.environ.get(API_KEY_ENV)
    if key:
        return key
    legacy = os.environ.get(LEGACY_API_KEY_ENV)
    if legacy:
        global _warned_legacy
        if not _warned_legacy:
            _log.warning(
                "HEXGATE_KEY is deprecated and will be removed in a future "
                "release; rename it to HEXGATE_API_KEY."
            )
            _warned_legacy = True
        return legacy
    return None
