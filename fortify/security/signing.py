"""Ed25519 signing + verification for policy bundles.

The platform signs bundle manifests; the SDK verifies them before
trusting the compiled wasm. This module is the SDK's half — primarily
``verify`` — plus keygen/sign helpers for local dev and tests.

Key conventions match the platform exactly so the two interoperate
without reformatting:

  * Keys are **raw 32-byte** Ed25519 (not PEM/DER). The platform's
    keystore emits ``public_bytes(Encoding.Raw, PublicFormat.Raw)`` and
    its JWKS endpoint publishes base64url of those 32 bytes — the same
    encoding the SDK already uses for ``FORTIFY_PUBLIC_KEY`` to verify
    biscuits. Bundle verification reuses that trust anchor.
  * Signatures are **raw 64-byte** Ed25519 (what ``private_key.sign()``
    returns).

So a bundle pulled from the platform is verified against the same key
the SDK already trusts for token signatures — one root, two artifacts.
"""

from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


class SignatureError(RuntimeError):
    """A signature did not verify against the given public key + payload."""


# ---------------------------------------------------------------------------
# Keygen
# ---------------------------------------------------------------------------


def generate_keypair() -> tuple[bytes, bytes]:
    """Return a fresh ``(private_key_raw, public_key_raw)`` Ed25519 pair.

    Both are raw bytes — 32-byte private seed, 32-byte public key — to
    match the platform keystore's wire format. The private bytes are
    sensitive: hand them straight to a signer, never log or transmit.
    """
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        encoding=Encoding.Raw,
        format=PrivateFormat.Raw,
        encryption_algorithm=NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        encoding=Encoding.Raw, format=PublicFormat.Raw
    )
    return private_raw, public_raw


def public_key_for(private_key_raw: bytes) -> bytes:
    """Derive the raw 32-byte public key from a raw private key."""
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_raw)
    return private_key.public_key().public_bytes(
        encoding=Encoding.Raw, format=PublicFormat.Raw
    )


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


def sign_bytes(data: bytes, private_key_raw: bytes) -> bytes:
    """Return a raw 64-byte Ed25519 signature over ``data``."""
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(private_key_raw)
    except ValueError as exc:
        raise SignatureError(f"invalid private key: {exc}") from exc
    return private_key.sign(data)


def verify_bytes(data: bytes, signature: bytes, public_key_raw: bytes) -> None:
    """Verify ``signature`` over ``data`` with ``public_key_raw``.

    Returns ``None`` on success; raises :class:`SignatureError` on any
    failure — bad key, malformed signature, or genuine mismatch. Callers
    that want a boolean can wrap this in a try/except.
    """
    try:
        public_key = Ed25519PublicKey.from_public_bytes(public_key_raw)
    except ValueError as exc:
        raise SignatureError(f"invalid public key: {exc}") from exc
    try:
        public_key.verify(signature, data)
    except InvalidSignature as exc:
        raise SignatureError("signature does not match payload") from exc


# ---------------------------------------------------------------------------
# Encoding helpers (base64url, matching the platform's JWKS + env wire format)
# ---------------------------------------------------------------------------


def encode_key(raw: bytes) -> str:
    """base64url-encode raw key/signature bytes (no padding), for text storage."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_key(encoded: str) -> bytes:
    """Decode a base64url string back to raw bytes, tolerating missing padding."""
    padded = encoded.strip() + "=" * (-len(encoded.strip()) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError) as exc:
        raise SignatureError(f"not valid base64url: {exc}") from exc
