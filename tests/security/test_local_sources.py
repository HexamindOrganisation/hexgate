"""Tests for the local policy sources (M2 phase 8b).

The platform path (PlatformPolicySource) is covered in ``test_source.py``.
These tests cover the two dev-loop sources:

  * :class:`BundleDirPolicySource` — refresh a pre-built bundle dir
    (output of ``fortify policy build``) by mtime.
  * :class:`YamlPolicySource` — recompile a ``policy.yaml`` on save.

Both need ``opa`` on PATH to actually compile a wasm module. Tests that
need it are skipped on environments without opa, but the no-opa
fallback path (graceful errors) is also exercised.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from fortify.security import (
    BundleDirPolicySource,
    PolicyBundle,
    YamlPolicySource,
    build_signed_bundle,
    generate_keypair,
    sign_bytes,
)

_OPA_AVAILABLE = shutil.which("opa") is not None
needs_opa = pytest.mark.skipif(not _OPA_AVAILABLE, reason="opa not on PATH")


def _permissive_sig_policy():
    """A SignaturePolicy that doesn't refuse anything — for dispatch tests
    that aren't exercising the signature matrix."""
    from fortify.security.source import SignaturePolicy

    return SignaturePolicy(verify_with=None, require_signature=False)


_DEMO_YAML = """\
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

_NEXT_YAML = """\
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
          - args.amount <= 1000
"""


def _write_bundle_dir(yaml_text: str, target: Path, *, sign=None) -> Path:
    """Materialize a ``fortify policy build``-shaped directory under ``target``.

    We reuse the SDK's ``build_signed_bundle`` instead of shelling out so
    these tests don't depend on the CLI; the on-disk layout is what
    :meth:`PolicyBundle.from_disk` expects.
    """
    target.mkdir(parents=True, exist_ok=True)
    built = build_signed_bundle(yaml_text, source_name="policy.yaml", sign=sign)
    (target / "policy.yaml").write_text(yaml_text, encoding="utf-8")
    (target / "policy.rego").write_text(built.rego_text, encoding="utf-8")
    assert built.wasm_bytes is not None
    (target / "policy.wasm").write_bytes(built.wasm_bytes)
    (target / "policy.bundle.json").write_bytes(built.manifest_bytes)
    if built.signature is not None:
        (target / "policy.bundle.json.sig").write_bytes(built.signature)
    return target / "policy.bundle.json"


# ---------------------------------------------------------------------------
# BundleDirPolicySource
# ---------------------------------------------------------------------------


@needs_opa
def test_bundle_dir_source_loads_and_enforces(tmp_path: Path) -> None:
    """A built directory loads + integrity-verifies + evaluates."""
    _write_bundle_dir(_DEMO_YAML, tmp_path)
    src = BundleDirPolicySource(tmp_path)
    bundle = src.fetch()
    assert isinstance(bundle, PolicyBundle)
    d = bundle.policy().decide(
        role="billing", tool="refund_order", args={"amount": 200}
    )
    assert d.allow is True


@needs_opa
def test_bundle_dir_source_reuses_instance_when_unchanged(tmp_path: Path) -> None:
    """No mtime change → fetch returns the SAME object (identity match
    means the runtime's refresh seam skips the swap)."""
    _write_bundle_dir(_DEMO_YAML, tmp_path)
    src = BundleDirPolicySource(tmp_path)
    a = src.fetch()
    b = src.fetch()
    assert a is b


@needs_opa
def test_bundle_dir_source_reloads_when_manifest_changes(tmp_path: Path) -> None:
    """Rewriting the bundle (e.g. fortify policy build again) → new instance."""
    manifest = _write_bundle_dir(_DEMO_YAML, tmp_path)
    src = BundleDirPolicySource(tmp_path)
    first = src.fetch()

    # Bump mtime explicitly so the test isn't sensitive to filesystem
    # timestamp granularity (mtime_ns is precise; touch+sleep would work
    # too, but is slower).
    _write_bundle_dir(_NEXT_YAML, tmp_path)
    future = manifest.stat().st_mtime_ns + 1_000_000_000  # +1s
    os.utime(manifest, ns=(future, future))

    second = src.fetch()
    assert second is not first
    assert second.wasm_hash != first.wasm_hash


@needs_opa
def test_bundle_dir_source_verifies_signature_when_pubkey_provided(
    tmp_path: Path,
) -> None:
    """A signed bundle + the matching pubkey → verifies cleanly."""
    priv, pub = generate_keypair()
    _write_bundle_dir(_DEMO_YAML, tmp_path, sign=lambda b: sign_bytes(b, priv))
    src = BundleDirPolicySource(tmp_path, verify_with=pub)
    bundle = src.fetch()
    assert bundle is not None and bundle.is_signed


@needs_opa
def test_bundle_dir_source_rejects_wrong_pubkey(tmp_path: Path) -> None:
    """Signed bundle + a stranger's pubkey → fail loudly, no silent downgrade."""
    priv, _ = generate_keypair()
    _, stranger = generate_keypair()
    _write_bundle_dir(_DEMO_YAML, tmp_path, sign=lambda b: sign_bytes(b, priv))
    src = BundleDirPolicySource(tmp_path, verify_with=stranger)
    with pytest.raises(RuntimeError, match="signature verification"):
        src.fetch()


def test_bundle_dir_source_rejects_missing_dir(tmp_path: Path) -> None:
    src = BundleDirPolicySource(tmp_path / "nope")
    with pytest.raises(RuntimeError, match="not a directory"):
        src.fetch()


def test_bundle_dir_source_rejects_empty_dir(tmp_path: Path) -> None:
    src = BundleDirPolicySource(tmp_path)
    with pytest.raises(RuntimeError, match="no \\*.bundle.json"):
        src.fetch()


# ---------------------------------------------------------------------------
# YamlPolicySource
# ---------------------------------------------------------------------------


@needs_opa
def test_yaml_source_compiles_and_enforces(tmp_path: Path) -> None:
    """First fetch compiles the yaml → a usable PolicyBundle."""
    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text(_DEMO_YAML, encoding="utf-8")
    src = YamlPolicySource(yaml_path)
    bundle = src.fetch()
    assert isinstance(bundle, PolicyBundle)
    assert not bundle.is_signed  # default: unsigned for dev loop
    d = bundle.policy().decide(
        role="billing", tool="refund_order", args={"amount": 100}
    )
    assert d.allow is True


@needs_opa
def test_yaml_source_reuses_instance_when_yaml_unchanged(tmp_path: Path) -> None:
    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text(_DEMO_YAML, encoding="utf-8")
    src = YamlPolicySource(yaml_path)
    a = src.fetch()
    b = src.fetch()
    assert a is b


@needs_opa
def test_yaml_source_recompiles_on_save(tmp_path: Path) -> None:
    """Edit policy.yaml + bump mtime → next fetch returns a new bundle
    reflecting the new constraints."""
    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text(_DEMO_YAML, encoding="utf-8")
    src = YamlPolicySource(yaml_path)
    first = src.fetch()
    # Old policy denies 700.
    assert (
        first.policy()
        .decide(role="billing", tool="refund_order", args={"amount": 700})
        .allow
        is False
    )

    yaml_path.write_text(_NEXT_YAML, encoding="utf-8")
    future = yaml_path.stat().st_mtime_ns + 1_000_000_000
    os.utime(yaml_path, ns=(future, future))

    second = src.fetch()
    assert second is not first
    # New policy raised the cap to 1000 → 700 now allowed.
    assert (
        second.policy()
        .decide(role="billing", tool="refund_order", args={"amount": 700})
        .allow
        is True
    )


@needs_opa
def test_yaml_source_signs_when_sign_callable_provided(tmp_path: Path) -> None:
    """When a ``sign`` callable is supplied the produced bundle is signed
    (so callers that gate on ``is_signed`` see what they expect)."""
    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text(_DEMO_YAML, encoding="utf-8")
    priv, pub = generate_keypair()
    src = YamlPolicySource(yaml_path, sign=lambda b: sign_bytes(b, priv))
    bundle = src.fetch()
    assert bundle is not None and bundle.is_signed
    # And the signature is verifiable against the public half.
    bundle.verify_signature(pub)


def test_yaml_source_missing_file_raises(tmp_path: Path) -> None:
    src = YamlPolicySource(tmp_path / "missing.yaml")
    with pytest.raises(RuntimeError, match="disappeared|could not be read"):
        src.fetch()


# ---------------------------------------------------------------------------
# loader dispatch — FORTIFY_LOCAL_POLICY shape-based routing
# ---------------------------------------------------------------------------


@needs_opa
def test_local_dispatch_routes_dir_to_bundle_dir_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fortify.security.source import _local_policy_source

    _write_bundle_dir(_DEMO_YAML, tmp_path)
    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(tmp_path))
    monkeypatch.delenv("FORTIFY_BUNDLE_REQUIRE_SIGNATURE", raising=False)
    monkeypatch.delenv("FORTIFY_BUNDLE_PUBKEY_PATH", raising=False)
    monkeypatch.delenv("FORTIFY_BUNDLE_SIGN_KEY_PATH", raising=False)

    src = _local_policy_source(_permissive_sig_policy())
    assert isinstance(src, BundleDirPolicySource)


@needs_opa
def test_local_dispatch_routes_yaml_to_yaml_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fortify.security.source import _local_policy_source

    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text(_DEMO_YAML, encoding="utf-8")
    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(yaml_path))
    monkeypatch.delenv("FORTIFY_BUNDLE_REQUIRE_SIGNATURE", raising=False)
    monkeypatch.delenv("FORTIFY_BUNDLE_PUBKEY_PATH", raising=False)
    monkeypatch.delenv("FORTIFY_BUNDLE_SIGN_KEY_PATH", raising=False)

    src = _local_policy_source(_permissive_sig_policy())
    assert isinstance(src, YamlPolicySource)


def test_local_dispatch_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from fortify.security.source import _local_policy_source

    monkeypatch.delenv("FORTIFY_LOCAL_POLICY", raising=False)
    assert _local_policy_source(_permissive_sig_policy()) is None


def test_local_dispatch_rejects_unknown_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing the env var at a random file (not yaml, not a bundle dir)
    is a configuration error, not a silent fallback."""
    from fortify.security.source import _local_policy_source

    target = tmp_path / "policy.txt"
    target.write_text("not yaml", encoding="utf-8")
    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(target))
    with pytest.raises(RuntimeError, match="expected a bundle"):
        _local_policy_source(_permissive_sig_policy())


@needs_opa
def test_local_override_returns_bundle_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The high-level helper returns BOTH the initial bundle (for
    enforce_policy) and the source (for runtime refresh)."""
    from fortify.security.source import _local_policy_override

    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text(_DEMO_YAML, encoding="utf-8")
    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(yaml_path))
    monkeypatch.delenv("FORTIFY_BUNDLE_REQUIRE_SIGNATURE", raising=False)

    out = _local_policy_override()
    assert out is not None
    bundle, source = out
    assert isinstance(bundle, PolicyBundle)
    # Protocol check is structural — assert the contract instead of
    # isinstance(PolicySource).
    assert hasattr(source, "fetch") and callable(source.fetch)
    # And the source returns the SAME bundle on a noop fetch.
    assert source.fetch() is bundle


def test_local_override_require_signature_rejects_unsigned_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQUIRE_SIGNATURE + yaml source without a sign-key → refuse,
    don't sleepwalk into running unsigned policy."""
    from fortify.security.source import _local_policy_override

    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text(_DEMO_YAML, encoding="utf-8")
    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", str(yaml_path))
    monkeypatch.setenv("FORTIFY_BUNDLE_REQUIRE_SIGNATURE", "true")
    monkeypatch.delenv("FORTIFY_BUNDLE_SIGN_KEY_PATH", raising=False)

    if not _OPA_AVAILABLE:
        # Without opa we never even reach the signature check — the
        # build inside YamlPolicySource will fail first. Skip rather
        # than assert a misleading message.
        pytest.skip("opa not on PATH")
    with pytest.raises(RuntimeError, match="REQUIRE_SIGNATURE"):
        _local_policy_override()
