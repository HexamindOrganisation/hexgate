"""Biscuit verification helpers for the SDK side.

The platform mints Biscuit tokens signed by its root Ed25519 keypair. The
SDK verifies them locally before trusting them for any call. We deliberately
mirror only the *verify-side* of the platform's wrapper — minting and
attenuation are platform/dev concerns and don't belong in the SDK runtime.

Two responsibilities live here:

1. Parse the human-readable envelope ``fty_<env>_<project>_<biscuit_b64>``
   into its parts. The Biscuit payload itself contains underscores
   (URL-safe base64), so the parser splits on the first three only.

2. Verify the signature chain against an expected public key. A token
   that was tampered with, signed by a different key, or just garbled
   raises :class:`TokenSignatureError`.

Defense in depth — the platform also verifies on the bearer-auth path,
but verifying client-side catches misconfiguration at startup instead of
on the first failed API call.
"""

from __future__ import annotations

ENVELOPE_PREFIX = "fty"


class TokenError(RuntimeError):
    """Base class for token-related failures."""


class TokenSignatureError(TokenError):
    """Raised when a Biscuit's signature does not chain to the expected key."""


def parse_envelope(envelope: str) -> tuple[str, str, str]:
    """Parse ``fty_<env>_<project>_<biscuit_b64>`` into ``(env, project, biscuit_b64)``.

    The biscuit base64 itself contains underscores (URL-safe), so we split
    on the first three underscores only — anything after the project segment
    is the Biscuit payload.
    """
    parts = envelope.split("_", 3)
    if len(parts) != 4 or parts[0] != ENVELOPE_PREFIX:
        raise TokenError(
            f"malformed Fortify token envelope (expected '{ENVELOPE_PREFIX}_<env>_<project>_<biscuit>')"
        )
    env, project_id, biscuit_b64 = parts[1], parts[2], parts[3]
    return env, project_id, biscuit_b64


def verify_biscuit(token_b64: str, public_key_bytes: bytes) -> None:
    """Verify a Biscuit's signature against ``public_key_bytes``.

    Raises :class:`TokenSignatureError` on tamper, malformed key, or
    corrupt payload. Returns nothing on success — verification means *the
    token was signed by the holder of the matching private key*, not that
    any specific policy permits it. The platform decides the latter.

    ``biscuit-python`` is imported lazily so importing this module doesn't
    pay the native-library load cost when the SDK is used purely offline
    (e.g., loading local agents without ``FORTIFY_KEY``).
    """
    from biscuit_auth import (
        Algorithm,
        Biscuit,
        BiscuitValidationError,
        PublicKey,
    )

    try:
        pub = PublicKey.from_bytes(public_key_bytes, Algorithm.Ed25519)
    except (ValueError, TypeError) as exc:
        raise TokenSignatureError(f"malformed public key: {exc}") from exc
    try:
        Biscuit.from_base64(token_b64, pub)
    except BiscuitValidationError as exc:
        raise TokenSignatureError(str(exc)) from exc
