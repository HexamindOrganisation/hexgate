"""Tests for platform-side bundle compilation + signing (M2 phase 7a).

The control plane compiles policy.yaml → WASM and signs the manifest at
save time. These tests cover the compile helper directly, the save path
through ``update_agent``, and the wire format the SDK will consume.

Compilation shells out to ``opa``; tests that need it are skipped when
opa isn't on PATH.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil

import pytest
from sqlmodel import Session, SQLModel, create_engine

import services
from fortify.security import generate_keypair, sign_bytes, verify_bytes
from models import Agent  # noqa: F401 — ensures the table is registered
from services import compile_bundle, ensure_default_project, update_agent


_OPA_AVAILABLE = shutil.which("opa") is not None
needs_opa = pytest.mark.skipif(
    not _OPA_AVAILABLE, reason="opa not on PATH"
)


_DEMO_POLICY = """\
version: 1
roles:
  default:
    tools:
      web_search: { mode: allow }
  billing:
    tools:
      refund_order:
        mode: allow
        constraints:
          - args.amount <= 500
"""

_BAD_POLICY = """\
version: 1
roles:
  default:
    tools:
      t:
        mode: allow
        constraints:
          - args.x ~~ 1
"""


@pytest.fixture
def signer() -> tuple:
    """A throwaway keypair + a sign callable shaped like keystore.sign."""
    private_raw, public_raw = generate_keypair()
    return (lambda data: sign_bytes(data, private_raw)), public_raw


@pytest.fixture
def session(tmp_path):
    """A fresh temp-file SQLite session with the schema + seeded agents."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        ensure_default_project(s)
        yield s


# ---------------------------------------------------------------------------
# compile_bundle
# ---------------------------------------------------------------------------


@needs_opa
def test_compile_bundle_produces_signed_artifact(signer) -> None:
    sign, public_raw = signer
    out = compile_bundle(_DEMO_POLICY, sign)
    assert out is not None
    wasm, manifest_text, signature = out
    assert wasm.startswith(b"\x00asm")
    # Signature verifies over the EXACT manifest bytes.
    verify_bytes(manifest_text.encode("utf-8"), signature, public_raw)


@needs_opa
def test_compile_bundle_manifest_hash_matches_wasm(signer) -> None:
    sign, _ = signer
    wasm, manifest_text, _ = compile_bundle(_DEMO_POLICY, sign)
    manifest = json.loads(manifest_text)
    assert manifest["wasm_hash"] == hashlib.sha256(wasm).hexdigest()


def test_compile_bundle_returns_none_for_bad_policy(signer) -> None:
    """A malformed constraint degrades to None — no bundle, no crash."""
    sign, _ = signer
    assert compile_bundle(_BAD_POLICY, sign) is None


def test_compile_bundle_returns_none_when_opa_missing(
    signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If opa isn't available, compile_bundle returns None (graceful degrade)."""
    from fortify.security.rego_wasm import OpaNotFoundError

    def boom(*_a, **_k):
        raise OpaNotFoundError("opa not found (simulated)")

    # Patch the symbol as imported inside compile_bundle's function scope.
    monkeypatch.setattr("fortify.security.compile_to_wasm", boom)
    sign, _ = signer
    assert compile_bundle(_DEMO_POLICY, sign) is None


# ---------------------------------------------------------------------------
# update_agent save path
# ---------------------------------------------------------------------------


@needs_opa
def test_update_agent_stores_signed_bundle(session, signer) -> None:
    sign, public_raw = signer
    agent = update_agent(
        session,
        services.DEFAULT_PROJECT_ID,
        "default",
        policy_yaml=_DEMO_POLICY,
        sign=sign,
    )
    assert agent is not None
    assert agent.compiled_wasm and agent.compiled_wasm.startswith(b"\x00asm")
    assert agent.bundle_manifest is not None
    assert agent.bundle_signature is not None
    verify_bytes(
        agent.bundle_manifest.encode("utf-8"), agent.bundle_signature, public_raw
    )


@needs_opa
def test_update_agent_clears_stale_bundle_on_bad_policy(session, signer) -> None:
    """A good save then a broken save drops the now-wrong bundle."""
    sign, _ = signer
    update_agent(
        session, services.DEFAULT_PROJECT_ID, "default",
        policy_yaml=_DEMO_POLICY, sign=sign,
    )
    agent = update_agent(
        session, services.DEFAULT_PROJECT_ID, "default",
        policy_yaml=_BAD_POLICY, sign=sign,
    )
    assert agent.compiled_wasm is None
    assert agent.bundle_manifest is None
    assert agent.bundle_signature is None


def test_update_agent_without_sign_stores_no_bundle(session) -> None:
    """No signer → no bundle (pure yaml save, e.g. tests / opa-less envs)."""
    agent = update_agent(
        session, services.DEFAULT_PROJECT_ID, "default",
        policy_yaml=_DEMO_POLICY,
    )
    assert agent.compiled_wasm is None


# ---------------------------------------------------------------------------
# Wire format (_agent_read serializer)
# ---------------------------------------------------------------------------


@needs_opa
def test_agent_read_serializes_bundle_as_base64(session, signer) -> None:
    import main

    sign, public_raw = signer
    agent = update_agent(
        session, services.DEFAULT_PROJECT_ID, "default",
        policy_yaml=_DEMO_POLICY, sign=sign,
    )
    view = main._agent_read(agent)
    assert view.bundle_wasm_b64 is not None
    assert view.bundle_signature_b64 is not None
    assert view.bundle_manifest is not None
    # Round-trip the wire format and verify the signature end to end.
    wasm = base64.b64decode(view.bundle_wasm_b64)
    sig = base64.b64decode(view.bundle_signature_b64)
    assert wasm.startswith(b"\x00asm")
    verify_bytes(view.bundle_manifest.encode("utf-8"), sig, public_raw)
    # And the manifest's wasm_hash still ties to the served wasm.
    assert json.loads(view.bundle_manifest)["wasm_hash"] == hashlib.sha256(wasm).hexdigest()


def test_agent_read_nulls_when_unsigned(session) -> None:
    agent = update_agent(
        session, services.DEFAULT_PROJECT_ID, "default",
        policy_yaml=_DEMO_POLICY,
    )
    import main

    view = main._agent_read(agent)
    assert view.bundle_wasm_b64 is None
    assert view.bundle_manifest is None
    assert view.bundle_signature_b64 is None
