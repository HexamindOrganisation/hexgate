"""Tests for the SDK-side Biscuit verification helpers."""

from __future__ import annotations

import pytest
from biscuit_auth import Algorithm, BiscuitBuilder, KeyPair, PrivateKey

from fortify.cloud.biscuit import (
    TokenError,
    TokenSignatureError,
    parse_envelope,
    verify_biscuit,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def keys() -> tuple[bytes, bytes]:
    """Fresh Ed25519 keypair as raw bytes ``(priv, pub)``."""
    kp = KeyPair()
    return kp.private_key.to_bytes(), kp.public_key.to_bytes()


def _mint_biscuit(priv_bytes: bytes, source: str = 'project("test");') -> str:
    """Mint a base64 Biscuit signed with ``priv_bytes``.

    Bypasses the platform's biscuits.py wrapper so SDK tests don't depend on
    platform code. Just enough to produce a valid token to verify against.
    """
    pk = PrivateKey.from_bytes(priv_bytes, Algorithm.Ed25519)
    return BiscuitBuilder(source).build(pk).to_base64()


# ---------------------------------------------------------------------------
# parse_envelope
# ---------------------------------------------------------------------------


def test_parse_envelope_returns_three_parts() -> None:
    """fty_<env>_<project>_<biscuit> → (env, project, biscuit)."""
    env, project, biscuit = parse_envelope("fty_live_support-bot_abc123")
    assert env == "live"
    assert project == "support-bot"
    assert biscuit == "abc123"


def test_parse_envelope_preserves_underscores_in_biscuit() -> None:
    """The biscuit b64 commonly contains `_` (URL-safe alphabet)."""
    biscuit_with_underscores = "EqQC_with_under_scores_in_payload"
    full = f"fty_live_proj_{biscuit_with_underscores}"
    _, _, parsed = parse_envelope(full)
    assert parsed == biscuit_with_underscores


def test_parse_envelope_preserves_hyphens_in_project_id() -> None:
    """Project ids commonly use hyphens (kebab-case)."""
    full = "fty_live_my-project-id_BISCUIT"
    _, project, _ = parse_envelope(full)
    assert project == "my-project-id"


def test_parse_envelope_supports_test_env() -> None:
    """The env segment isn't restricted to `live` — `test` works too."""
    env, _, _ = parse_envelope("fty_test_proj_BISCUIT")
    assert env == "test"


def test_parse_envelope_rejects_missing_fty_prefix() -> None:
    """An envelope without `fty_` is not a Fortify token."""
    with pytest.raises(TokenError):
        parse_envelope("not_live_proj_biscuit")


def test_parse_envelope_rejects_too_few_segments() -> None:
    """Envelopes that don't have all four underscore segments are malformed."""
    with pytest.raises(TokenError):
        parse_envelope("fty_live_onlythree")


def test_parse_envelope_rejects_empty_string() -> None:
    """An empty input is not a valid envelope."""
    with pytest.raises(TokenError):
        parse_envelope("")


# ---------------------------------------------------------------------------
# verify_biscuit
# ---------------------------------------------------------------------------


def test_verify_accepts_token_signed_by_matching_key(keys: tuple[bytes, bytes]) -> None:
    """A token signed by the private key verifies with its public key."""
    priv, pub = keys
    biscuit = _mint_biscuit(priv)
    verify_biscuit(biscuit, pub)  # no exception


def test_verify_rejects_token_signed_by_other_key(keys: tuple[bytes, bytes]) -> None:
    """A token signed by one key must not verify against another."""
    priv, _ = keys
    other_pub = KeyPair().public_key.to_bytes()
    biscuit = _mint_biscuit(priv)
    with pytest.raises(TokenSignatureError):
        verify_biscuit(biscuit, other_pub)


def test_verify_rejects_malformed_public_key(keys: tuple[bytes, bytes]) -> None:
    """Wrong-length public key bytes are rejected with a clean error."""
    priv, _ = keys
    biscuit = _mint_biscuit(priv)
    with pytest.raises(TokenSignatureError, match="malformed public key"):
        verify_biscuit(biscuit, b"too short")


def test_verify_rejects_garbage_token(keys: tuple[bytes, bytes]) -> None:
    """Random non-base64 bytes don't get accepted as a token."""
    _, pub = keys
    with pytest.raises(TokenSignatureError):
        verify_biscuit("not even base64 ! @#$", pub)


def test_verify_rejects_tampered_payload(keys: tuple[bytes, bytes]) -> None:
    """Flipping a few bytes of the encoded biscuit breaks verification."""
    priv, pub = keys
    good = _mint_biscuit(priv)
    # Tamper near the end (avoiding base64 padding) — keeps the b64 valid
    # so the failure surfaces from signature check, not malformed payload.
    tampered = good[: len(good) - 6] + "AAAA" + good[-2:]
    with pytest.raises(TokenSignatureError):
        verify_biscuit(tampered, pub)
