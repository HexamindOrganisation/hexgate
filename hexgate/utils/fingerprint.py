"""Short, log-safe IDs for values that must never be printed."""

from __future__ import annotations

import hashlib
from typing import Final

_FINGERPRINT_LENGTH: Final = 12
_FINGERPRINT_ENCODING: Final = "utf-8"


def fingerprint(value: str) -> str:
    """A sha256 prefix of ``value``. Log and audit lines share it, so they can be
    correlated without carrying the value."""
    # surrogatepass: a lone surrogate must print, not raise inside a log call.
    digest = hashlib.sha256(value.encode(_FINGERPRINT_ENCODING, "surrogatepass"))
    return digest.hexdigest()[:_FINGERPRINT_LENGTH]
