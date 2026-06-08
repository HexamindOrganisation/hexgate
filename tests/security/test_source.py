"""Tests for the policy source layer (M2 phase 8a).

``PolicySource`` is the seam that lets the agent runtime refresh policy
at every run without caring whether the bundle came from the platform or
from disk. These tests cover :class:`PlatformPolicySource` (HTTP +
``If-None-Match`` / 304) — local sources land in phase 8b.

Each test uses a tiny in-memory ``FakeClient`` rather than spinning the
real platform, so we control exactly what the platform "served" and on
which call.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest

from fortify.security import (
    PolicyBundle,
    compile_to_rego,
    compile_to_wasm,
    generate_keypair,
    sign_bytes,
)
from fortify.security.source import PlatformPolicySource, _warn_if_unverified


_OPA_AVAILABLE = shutil.which("opa") is not None
needs_opa = pytest.mark.skipif(not _OPA_AVAILABLE, reason="opa not on PATH")


# ---------------------------------------------------------------------------
# _warn_if_unverified — binding-path counterpart to the loader's
# SignaturePolicy.warn_if_unverified (same signal for a signed local bundle).
# ---------------------------------------------------------------------------


def test_warn_if_unverified_warns_for_signed_bundle_without_pubkey(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Signed bundle + no pubkey configured → warn that it wasn't verified."""
    monkeypatch.delenv("FORTIFY_BUNDLE_PUBKEY_PATH", raising=False)

    _warn_if_unverified(SimpleNamespace(is_signed=True))

    err = capsys.readouterr().err
    assert "signature NOT verified" in err
    assert "FORTIFY_BUNDLE_PUBKEY_PATH" in err


def test_warn_if_unverified_silent_when_pubkey_configured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pubkey is configured → BundleDir verified on load; no warning."""
    monkeypatch.setenv("FORTIFY_BUNDLE_PUBKEY_PATH", "/some/key.public")

    _warn_if_unverified(SimpleNamespace(is_signed=True))

    assert capsys.readouterr().err == ""


def test_warn_if_unverified_silent_for_unsigned_bundle(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unsigned bundle → nothing to verify, nothing to warn about."""
    monkeypatch.delenv("FORTIFY_BUNDLE_PUBKEY_PATH", raising=False)

    _warn_if_unverified(SimpleNamespace(is_signed=False))

    assert capsys.readouterr().err == ""


_POLICY_PAYLOAD = {
    "version": 1,
    "roles": {
        "billing": {
            "tools": {
                "refund_order": {"mode": "allow", "constraints": ["args.amount <= 500"]}
            }
        }
    },
}


def _bundle_response(private_raw: bytes, amount_cap: int = 500) -> dict:
    """Build a get_agent-shaped response carrying a real signed bundle."""
    payload = {
        "version": 1,
        "roles": {
            "billing": {
                "tools": {
                    "refund_order": {
                        "mode": "allow",
                        "constraints": [f"args.amount <= {amount_cap}"],
                    }
                }
            }
        },
    }
    rego = compile_to_rego(payload)
    wasm = compile_to_wasm(rego).wasm
    manifest = {
        "version": 1,
        "source": "policy.yaml",
        "source_hash": "0" * 64,
        "rego_hash": hashlib.sha256(rego.encode("utf-8")).hexdigest(),
        "wasm_hash": hashlib.sha256(wasm).hexdigest(),
    }
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    signature = sign_bytes(manifest_text.encode("utf-8"), private_raw)
    return {
        "bundle_wasm_b64": base64.b64encode(wasm).decode("ascii"),
        "bundle_manifest": manifest_text,
        "bundle_signature_b64": base64.b64encode(signature).decode("ascii"),
    }


class _FakeClient:
    """Stand-in for FortifyClient that scripts a sequence of responses.

    ``serve(...)`` queues the next answer the client should return; each
    ``get_agent`` consumes one. The 304 path is triggered when the
    incoming ``If-None-Match`` matches the previously-served etag.
    """

    def __init__(self, public_raw: bytes) -> None:
        self._public_raw = public_raw
        self._queued: list[tuple[dict | None, str | None]] = []
        self.calls: list[str | None] = []

    def serve(self, payload: dict | None, etag: str | None) -> None:
        self._queued.append((payload, etag))

    def serve_304(self, etag: str) -> None:
        self._queued.append((None, etag))

    def get_agent(
        self, _name: str, *, if_none_match: str | None = None
    ) -> tuple[dict | None, str | None]:
        self.calls.append(if_none_match)
        return self._queued.pop(0)

    def public_key_bytes(self) -> bytes:
        return self._public_raw


# ---------------------------------------------------------------------------
# PlatformPolicySource
# ---------------------------------------------------------------------------


@needs_opa
def test_first_fetch_returns_verified_bundle() -> None:
    """A 200 response builds, verifies, caches, and returns the bundle."""
    priv, pub = generate_keypair()
    fc = _FakeClient(pub)
    fc.serve(_bundle_response(priv), etag='"abc123"')
    src = PlatformPolicySource(fc, "default")

    bundle = src.fetch()
    assert isinstance(bundle, PolicyBundle)
    assert bundle.is_signed
    # First call has no etag to send.
    assert fc.calls == [None]


@needs_opa
def test_second_fetch_sends_etag_and_304_reuses_cache() -> None:
    """304 returns the SAME instance — no decode, no signature verify."""
    priv, pub = generate_keypair()
    fc = _FakeClient(pub)
    payload = _bundle_response(priv)
    fc.serve(payload, etag='"abc123"')
    fc.serve_304('"abc123"')

    src = PlatformPolicySource(fc, "default")
    first = src.fetch()
    second = src.fetch()

    assert first is second  # identity match — same Python object reused
    # Second call sent the etag we got from the first.
    assert fc.calls == [None, '"abc123"']


@needs_opa
def test_bundle_change_returns_new_instance() -> None:
    """A different wasm_hash → new bundle returned + new etag tracked."""
    priv, pub = generate_keypair()
    fc = _FakeClient(pub)
    fc.serve(_bundle_response(priv, amount_cap=500), etag='"hash-a"')
    fc.serve(_bundle_response(priv, amount_cap=1000), etag='"hash-b"')

    src = PlatformPolicySource(fc, "default")
    first = src.fetch()
    second = src.fetch()

    assert first is not second
    assert first.wasm_hash != second.wasm_hash
    assert fc.calls == [None, '"hash-a"']  # second call sent first's etag


def test_no_bundle_in_payload_returns_none() -> None:
    """When the platform served no compiled bundle, fetch() → None."""
    _, pub = generate_keypair()
    fc = _FakeClient(pub)
    fc.serve({"agent_yaml": "name: x\n", "policy_yaml": "version: 1\n"}, etag=None)

    src = PlatformPolicySource(fc, "default")
    assert src.fetch() is None


@needs_opa
def test_wrong_pubkey_raises() -> None:
    """A bundle signed by one key, served with a stranger's pubkey on the
    client, fails verification at fetch — never silently downgraded."""
    priv, _ = generate_keypair()
    _, stranger = generate_keypair()
    fc = _FakeClient(stranger)
    fc.serve(_bundle_response(priv), etag=None)

    src = PlatformPolicySource(fc, "default")
    with pytest.raises(RuntimeError, match="failed verification"):
        src.fetch()


@needs_opa
def test_pre_seeded_source_skips_first_round_trip() -> None:
    """When constructed with initial_bundle + initial_etag, the first
    fetch sends If-None-Match and trivially hits 304."""
    priv, pub = generate_keypair()
    fc = _FakeClient(pub)
    # Pre-build a bundle the same way load_fortify_agent would have.
    from fortify.security.source import decode_and_verify_platform_bundle

    initial = decode_and_verify_platform_bundle(_bundle_response(priv), pub)
    fc.serve_304(f'"{initial.wasm_hash}"')

    src = PlatformPolicySource(
        fc, "default", initial_bundle=initial, initial_etag=f'"{initial.wasm_hash}"'
    )
    result = src.fetch()

    assert result is initial
    assert fc.calls == [f'"{initial.wasm_hash}"']


def test_concurrent_fetches_serialize() -> None:
    """The source owns its cache, so it owns its lock: two threads fetching
    at once never overlap inside fetch() (cached bundle/etag can't interleave)."""
    import threading
    import time

    _, pub = generate_keypair()

    class _SlowOverlapClient(_FakeClient):
        def __init__(self, public_raw: bytes) -> None:
            super().__init__(public_raw)
            self._gauge = threading.Lock()
            self.active = 0
            self.max_active = 0

        def get_agent(self, name, *, if_none_match=None):
            with self._gauge:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.02)  # widen the window an overlap would need
            try:
                return super().get_agent(name, if_none_match=if_none_match)
            finally:
                with self._gauge:
                    self.active -= 1

    fc = _SlowOverlapClient(pub)
    # Bundle-less payloads keep this opa-free; fetch() returns None for both.
    fc.serve({"policy_yaml": "version: 1\n"}, etag='"a"')
    fc.serve({"policy_yaml": "version: 1\n"}, etag='"b"')
    src = PlatformPolicySource(fc, "default")

    threads = [threading.Thread(target=src.fetch) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert all(not t.is_alive() for t in threads)
    assert fc.max_active == 1  # serialized — never two fetches in flight
