"""End-to-end tests for the ``FORTIFY_LOCAL_POLICY`` env override.

When the env var points at a valid bundle, loaders must:

  * verify the bundle's integrity,
  * attach it to the constructed agent's tools (in place of whatever
    policy.yaml the agent definition would have used),
  * leave a loud stderr trail so the dev knows the override is active.

Bad overrides (missing dir, hash mismatch, no wasm) must raise at load
time — silent fallback to the original policy would be a security
footgun. These tests build real bundles via ``opa`` and are skipped
when opa isn't available.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from fortify.agents import loader
from fortify.security import (
    compile_to_rego,
    compile_to_wasm,
    encode_key,
    generate_keypair,
    sign_bytes,
)


_OPA_AVAILABLE = shutil.which("opa") is not None
needs_opa = pytest.mark.skipif(
    not _OPA_AVAILABLE,
    reason="opa not on PATH — install via `brew install opa` to run these tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_OVERRIDE_YAML = """\
version: 1
roles:
  default:
    tools:
      web_search: { mode: allow }
"""


def _build_bundle_dir(directory: Path) -> Path:
    """Build a real bundle on disk inside ``directory`` and return that path."""
    directory.mkdir(parents=True, exist_ok=True)
    yaml_path = directory / "policy.yaml"
    yaml_path.write_text(_OVERRIDE_YAML, encoding="utf-8")
    source_hash = hashlib.sha256(_OVERRIDE_YAML.encode("utf-8")).hexdigest()

    rego = compile_to_rego(
        {
            "version": 1,
            "roles": {
                "default": {
                    "tools": {"web_search": {"mode": "allow"}},
                }
            },
        },
        source_hash=source_hash,
    )
    wasm = compile_to_wasm(rego).wasm

    (directory / "policy.rego").write_text(rego, encoding="utf-8")
    (directory / "policy.wasm").write_bytes(wasm)
    manifest = {
        "version": 1,
        "source": "policy.yaml",
        "source_hash": source_hash,
        "rego_hash": hashlib.sha256(rego.encode("utf-8")).hexdigest(),
        "wasm_hash": hashlib.sha256(wasm).hexdigest(),
    }
    (directory / "policy.bundle.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return directory


def _sign_bundle_dir(directory: Path, private_key_raw: bytes) -> None:
    """Write a detached signature over the manifest bytes in ``directory``."""
    manifest_bytes = (directory / "policy.bundle.json").read_bytes()
    sig = sign_bytes(manifest_bytes, private_key_raw)
    (directory / "policy.bundle.json.sig").write_bytes(sig)


def _write_pubkey(path: Path, public_key_raw: bytes) -> Path:
    """Write a base64url public key file (FORTIFY_BUNDLE_PUBKEY_PATH format)."""
    path.write_text(encode_key(public_key_raw) + "\n", encoding="utf-8")
    return path


def _clear_signature_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORTIFY_BUNDLE_PUBKEY_PATH", raising=False)
    monkeypatch.delenv("FORTIFY_BUNDLE_REQUIRE_SIGNATURE", raising=False)


# ---------------------------------------------------------------------------
# _local_policy_override (unit-level)
# ---------------------------------------------------------------------------


def test_override_returns_none_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env var → no override. Pure pydantic path keeps running."""
    monkeypatch.delenv("FORTIFY_LOCAL_POLICY", raising=False)
    assert loader._local_policy_override() is None


@needs_opa
def test_override_loads_bundle_when_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Env var pointing at a fresh bundle yields a verified PolicyBundle."""
    bundle_dir = _build_bundle_dir(tmp_path / "bundle")
    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(bundle_dir))

    bundle = loader._local_policy_override()
    assert bundle is not None
    # The override prints a loud announcement to stderr.
    err = capsys.readouterr().err
    assert "FORTIFY_LOCAL_POLICY active" in err


def test_override_raises_for_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing at a non-existent directory fails loudly, not silently."""
    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(tmp_path / "nope"))
    with pytest.raises(RuntimeError, match="bundle could not be loaded"):
        loader._local_policy_override()


@needs_opa
def test_override_raises_for_tampered_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle whose hashes don't match its manifest is rejected at load time."""
    bundle_dir = _build_bundle_dir(tmp_path / "bundle")
    # Tamper with the rego file after build — manifest still records the
    # original hash, so verify_integrity should reject this.
    (bundle_dir / "policy.rego").write_text(
        "package fortify.policy\n# tampered\n", encoding="utf-8"
    )
    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(bundle_dir))
    with pytest.raises(RuntimeError, match="hash mismatch"):
        loader._local_policy_override()


# ---------------------------------------------------------------------------
# Integration with the agent loaders
# ---------------------------------------------------------------------------


@needs_opa
def test_load_builtin_agent_picks_up_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_builtin_agent`` forwards the bundle to ``enforce_policy`` when
    the env var is set, replacing the agent's own policy.yaml."""
    from typing import Any
    from fortify.security import PolicyBundle

    bundle_dir = _build_bundle_dir(tmp_path / "bundle")
    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(bundle_dir))

    captured: dict[str, Any] = {}

    def fake_create_agent(**kwargs: Any) -> tuple[str, str]:
        return "agent", "handler"

    def fake_enforce_policy(_agent: Any, policy: Any, *, approval_handler: Any = None) -> Any:
        captured["policy"] = policy
        return _agent

    monkeypatch.setattr(loader, "create_agent", fake_create_agent)
    monkeypatch.setattr(loader, "enforce_policy", fake_enforce_policy)

    loader.load_builtin_agent("researcher")
    assert isinstance(captured["policy"], PolicyBundle), (
        "load_builtin_agent should have substituted the env-var bundle "
        f"for the on-disk policy.yaml; got {type(captured['policy'])}"
    )


@needs_opa
def test_load_builtin_agent_uses_original_policy_without_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check: without the env var, the agent's bundled policy.yaml
    is used (not a PolicyBundle). Guards against my own dispatcher
    silently always preferring wasm."""
    from typing import Any
    from fortify.security import AgentPolicy, PolicyBundle

    monkeypatch.delenv("FORTIFY_LOCAL_POLICY", raising=False)
    captured: dict[str, Any] = {}

    def fake_create_agent(**kwargs: Any) -> tuple[str, str]:
        return "agent", "handler"

    def fake_enforce_policy(_agent: Any, policy: Any, *, approval_handler: Any = None) -> Any:
        captured["policy"] = policy
        return _agent

    monkeypatch.setattr(loader, "create_agent", fake_create_agent)
    monkeypatch.setattr(loader, "enforce_policy", fake_enforce_policy)

    loader.load_builtin_agent("researcher")
    assert isinstance(captured["policy"], AgentPolicy)
    assert not isinstance(captured["policy"], PolicyBundle)


# ---------------------------------------------------------------------------
# Signature enforcement (FORTIFY_BUNDLE_PUBKEY_PATH / REQUIRE_SIGNATURE)
# ---------------------------------------------------------------------------


@needs_opa
def test_unsigned_bundle_loads_with_warning_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Default (no REQUIRE): an unsigned bundle loads but warns on stderr."""
    _clear_signature_env(monkeypatch)
    bundle_dir = _build_bundle_dir(tmp_path / "bundle")
    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(bundle_dir))

    bundle = loader._local_policy_override()
    assert bundle is not None
    err = capsys.readouterr().err
    assert "unsigned" in err


@needs_opa
def test_unsigned_bundle_refused_when_signature_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQUIRE_SIGNATURE=true rejects an unsigned bundle at load time."""
    _clear_signature_env(monkeypatch)
    bundle_dir = _build_bundle_dir(tmp_path / "bundle")
    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(bundle_dir))
    monkeypatch.setenv("FORTIFY_BUNDLE_REQUIRE_SIGNATURE", "true")

    with pytest.raises(RuntimeError, match="has no signature"):
        loader._local_policy_override()


@needs_opa
def test_signed_bundle_verifies_against_pubkey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A signed bundle + matching pubkey loads cleanly, even with REQUIRE on."""
    _clear_signature_env(monkeypatch)
    private_raw, public_raw = generate_keypair()
    bundle_dir = _build_bundle_dir(tmp_path / "bundle")
    _sign_bundle_dir(bundle_dir, private_raw)
    pubkey_path = _write_pubkey(tmp_path / "key.public", public_raw)

    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(bundle_dir))
    monkeypatch.setenv("FORTIFY_BUNDLE_PUBKEY_PATH", str(pubkey_path))
    monkeypatch.setenv("FORTIFY_BUNDLE_REQUIRE_SIGNATURE", "true")

    bundle = loader._local_policy_override()
    assert bundle is not None and bundle.is_signed
    err = capsys.readouterr().err
    assert "signed" in err


@needs_opa
def test_signed_bundle_wrong_pubkey_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signature that doesn't match the configured pubkey is refused."""
    _clear_signature_env(monkeypatch)
    signer_priv, _ = generate_keypair()
    _, stranger_pub = generate_keypair()
    bundle_dir = _build_bundle_dir(tmp_path / "bundle")
    _sign_bundle_dir(bundle_dir, signer_priv)
    pubkey_path = _write_pubkey(tmp_path / "key.public", stranger_pub)

    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(bundle_dir))
    monkeypatch.setenv("FORTIFY_BUNDLE_PUBKEY_PATH", str(pubkey_path))

    with pytest.raises(RuntimeError, match="failed signature verification"):
        loader._local_policy_override()


@needs_opa
def test_signed_bundle_no_pubkey_required_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signed bundle + REQUIRE on + no pubkey configured → refuse (no key to check)."""
    _clear_signature_env(monkeypatch)
    private_raw, _ = generate_keypair()
    bundle_dir = _build_bundle_dir(tmp_path / "bundle")
    _sign_bundle_dir(bundle_dir, private_raw)

    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(bundle_dir))
    monkeypatch.setenv("FORTIFY_BUNDLE_REQUIRE_SIGNATURE", "true")

    with pytest.raises(RuntimeError, match="is unset"):
        loader._local_policy_override()
