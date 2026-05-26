"""``PolicyBundle`` — a loadable, integrity-checked policy artifact.

A bundle is what ``fortify policy build`` emits: a directory containing
the source ``policy.yaml``, the compiled ``policy.rego``, the
``policy.wasm`` blob, and a ``policy.bundle.json`` manifest holding the
content hashes. This class wraps the on-disk layout so the runtime can
load + verify + evaluate in one motion.

Today the bundle is content-addressed by sha256 (hashes in the manifest
match sha256 of the on-disk artifacts). M2 phase 6 layers an Ed25519
signature on top: the platform signs the manifest, the runtime checks
the signature before trusting the hashes. Until then, integrity is a
local check — useful for "bundle wasn't corrupted in transit" but not
a defense against malicious authors.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from fortify.security.signing import SignatureError, verify_bytes
from fortify.security.wasm_engine import WasmPolicy


class BundleIntegrityError(RuntimeError):
    """A bundle's on-disk content does not match the hashes in its manifest.

    Examples: someone hand-edited ``policy.rego`` after the build, the
    wasm file was truncated in transit, or the manifest was swapped with
    one from a different build.
    """


class BundleLoadError(RuntimeError):
    """A bundle directory is missing required files or is malformed."""


class BundleSignatureError(RuntimeError):
    """A bundle's signature is missing, malformed, or fails verification.

    Distinct from :class:`BundleIntegrityError`: integrity is the local
    hash chain (files match the manifest); signature is *authenticity*
    (the manifest was signed by a key the runtime trusts). A bundle can
    have valid integrity but a bad/absent signature.
    """


@dataclass
class PolicyBundle:
    """A loaded policy bundle, ready to evaluate via WASM.

    The dataclass holds the raw bytes / text as loaded from disk; the
    ``WasmPolicy`` instance is created lazily on first ``policy()`` call
    so callers that only want metadata (e.g. the dashboard's bundle
    inspector) don't pay the wasmtime setup cost.
    """

    source_path: Path
    rego_text: str
    wasm_bytes: bytes
    manifest: dict
    # Exact on-disk manifest bytes — what the signature is computed over.
    # We keep the raw bytes (not a re-serialization of `manifest`) so
    # signature verification never depends on JSON canonicalization.
    manifest_bytes: bytes = b""
    # Detached signature over `manifest_bytes` (raw 64-byte Ed25519), or
    # None for unsigned bundles (dev / FORTIFY_LOCAL_POLICY path).
    signature: bytes | None = None
    _wasm_policy: WasmPolicy | None = field(default=None, repr=False, compare=False)

    # ---- Construction --------------------------------------------------

    @classmethod
    def from_disk(cls, path: Path | str) -> "PolicyBundle":
        """Load a bundle from a directory.

        Expects ``<dir>/{stem}.yaml``, ``{stem}.rego``, ``{stem}.wasm``,
        and ``{stem}.bundle.json`` for some shared ``{stem}`` (the
        source basename — e.g. "policy" for the platform's default
        export). When multiple stems are present in one directory we
        refuse rather than guess — pass an explicit file instead.
        """
        directory = Path(path)
        if not directory.is_dir():
            raise BundleLoadError(f"not a directory: {directory}")

        manifests = sorted(directory.glob("*.bundle.json"))
        if not manifests:
            raise BundleLoadError(
                f"no *.bundle.json found in {directory}; "
                "build with `fortify policy build` first."
            )
        if len(manifests) > 1:
            raise BundleLoadError(
                f"multiple bundle manifests in {directory}: "
                f"{[m.name for m in manifests]} — pass an explicit path."
            )
        manifest_path = manifests[0]
        # `foo.bundle.json` → stem = `foo`
        stem = manifest_path.name[: -len(".bundle.json")]

        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BundleLoadError(f"cannot read {manifest_path}: {exc}") from exc

        source_path = directory / f"{stem}.yaml"
        rego_path = directory / f"{stem}.rego"
        wasm_path = directory / f"{stem}.wasm"

        for required in (source_path, rego_path, wasm_path):
            if not required.is_file():
                raise BundleLoadError(
                    f"bundle at {directory} is missing {required.name}"
                )

        # Detached signature is optional — present only for platform-signed
        # (or `--sign-key`-built) bundles. Unsigned bundles load fine; the
        # caller decides whether to require a signature.
        sig_path = directory / f"{stem}.bundle.json.sig"
        signature = sig_path.read_bytes() if sig_path.is_file() else None

        return cls(
            source_path=source_path,
            rego_text=rego_path.read_text(encoding="utf-8"),
            wasm_bytes=wasm_path.read_bytes(),
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            signature=signature,
        )

    # ---- Integrity -----------------------------------------------------

    def verify_integrity(self) -> None:
        """Confirm every on-disk artifact matches the hash in the manifest.

        This is the *integrity* check (files match the manifest). For
        *authenticity* (the manifest was signed by a trusted key), see
        :meth:`verify_signature`. Run both for full assurance: signature
        proves the manifest is genuine, integrity proves the files match
        the manifest.
        """
        expected_source_hash = self.manifest.get("source_hash")
        if expected_source_hash:
            actual = hashlib.sha256(
                self.source_path.read_bytes()
            ).hexdigest()
            if actual != expected_source_hash:
                raise BundleIntegrityError(
                    f"source.yaml hash mismatch: manifest says "
                    f"{expected_source_hash}, got {actual}"
                )

        expected_rego_hash = self.manifest.get("rego_hash")
        if expected_rego_hash:
            actual = hashlib.sha256(self.rego_text.encode("utf-8")).hexdigest()
            if actual != expected_rego_hash:
                raise BundleIntegrityError(
                    f"rego hash mismatch: manifest says "
                    f"{expected_rego_hash}, got {actual}"
                )

        expected_wasm_hash = self.manifest.get("wasm_hash")
        if expected_wasm_hash is None:
            # Bundles built with `--no-wasm` carry None here — they can't
            # be used for enforcement.
            raise BundleIntegrityError(
                "manifest has no wasm_hash — this bundle was built without "
                "the WASM step (--no-wasm); rebuild with opa available."
            )
        actual = hashlib.sha256(self.wasm_bytes).hexdigest()
        if actual != expected_wasm_hash:
            raise BundleIntegrityError(
                f"wasm hash mismatch: manifest says {expected_wasm_hash}, "
                f"got {actual}"
            )

    # ---- Authenticity --------------------------------------------------

    @property
    def is_signed(self) -> bool:
        """Whether a detached signature was loaded alongside this bundle."""
        return self.signature is not None

    def verify_signature(self, public_key_raw: bytes) -> None:
        """Verify the detached signature over the manifest bytes.

        ``public_key_raw`` is the raw 32-byte Ed25519 public key — the
        same key the SDK trusts for biscuit verification (the platform
        signs both with one root). Raises :class:`BundleSignatureError`
        if there's no signature, or if it doesn't verify.

        Signature authenticates the manifest; the manifest's hashes
        authenticate the files. So a valid signature + a passing
        :meth:`verify_integrity` together prove the whole bundle came
        from the trusted signer untampered. Callers should run both.
        """
        if self.signature is None:
            raise BundleSignatureError(
                "bundle has no signature (no *.bundle.json.sig alongside the "
                "manifest); cannot verify authenticity."
            )
        try:
            verify_bytes(self.manifest_bytes, self.signature, public_key_raw)
        except SignatureError as exc:
            raise BundleSignatureError(
                f"bundle signature verification failed: {exc}"
            ) from exc

    # ---- Evaluation ----------------------------------------------------

    def policy(self) -> WasmPolicy:
        """Return the cached WasmPolicy (instantiated on first call)."""
        if self._wasm_policy is None:
            self._wasm_policy = WasmPolicy.from_bytes(self.wasm_bytes)
        return self._wasm_policy

    # ---- Metadata ------------------------------------------------------

    @property
    def source_hash(self) -> str | None:
        return self.manifest.get("source_hash")

    @property
    def wasm_hash(self) -> str | None:
        return self.manifest.get("wasm_hash")
