"""Policy sources — abstractions over "where the current policy lives."

The runtime fetches a :class:`PolicyBundle` (or ``None``) from a source at
every agent run; the source decides whether that's cheap or not. Three
implementations cover the production + local-dev workflows:

  * :class:`PlatformPolicySource` — HTTP fetch with ``If-None-Match`` /
    ``304 Not Modified``, so unchanged bundles cost one tiny round trip
    instead of a full payload + signature verify + wasm re-instantiation.
  * :class:`BundleDirPolicySource` — refresh a pre-built bundle directory
    on disk (today's ``FORTIFY_LOCAL_POLICY=<dir>`` path, made mtime-aware
    so a rebuild via ``fortify policy build`` takes effect on the next run).
  * :class:`YamlPolicySource` — auto-recompile a ``policy.yaml`` when its
    mtime changes. The dev edits → saves → runs loop matches the platform's
    hot-reload UX without the platform in the loop.

The common interface is :class:`PolicySource`; the agent runtime depends
on the protocol, not on any concrete type.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from fortify.security.bundle import (
    BundleIntegrityError,
    BundleLoadError,
    BundleSignatureError,
    PolicyBundle,
    build_signed_bundle,
)
from fortify.security.signing import SignatureError, decode_key

if TYPE_CHECKING:
    from collections.abc import Callable

    from fortify.cloud.client import FortifyClient


logger = logging.getLogger("fortify.security.source")


class PolicySource(Protocol):
    """Produces a current :class:`PolicyBundle` (or ``None``) on demand.

    Implementations are expected to be **cheap when nothing has changed**
    — caching, ETags, or mtime checks — so the agent runtime can call
    :meth:`fetch` at the top of every run without measurable cost.

    A returned ``None`` means "no bundle is configured for this source"
    (e.g. the platform served no compiled bundle). Callers fall back to
    whatever they had before (pydantic engine on raw YAML).
    """

    def fetch(self) -> PolicyBundle | None: ...


class PlatformPolicySource:
    """Pull + verify a signed bundle from the platform, with ETag/304.

    Holds the last seen bundle and its ``wasm_hash`` (the ETag the
    platform serves). Each :meth:`fetch` sends ``If-None-Match`` and:

      * ``304`` → returns the cached bundle without touching wasmtime or
        the signature path.
      * ``200`` → decodes + verifies the new payload, caches it, returns.
      * payload with no bundle → returns ``None`` (the platform couldn't
        compile, e.g. opa missing on the control plane — the SDK then
        falls back to its pydantic engine).

    Verification fails are fatal (a tampered platform bundle is never
    silently downgraded). The signature is checked against the same
    public key the SDK already trusts for biscuit verification.
    """

    def __init__(
        self,
        client: "FortifyClient",
        agent_name: str,
        *,
        initial_bundle: PolicyBundle | None = None,
        initial_etag: str | None = None,
    ) -> None:
        self._client = client
        self._agent_name = agent_name
        # Pre-seed when the caller already fetched + verified the bundle
        # (typical at agent load time). Avoids a redundant 200 round-trip
        # on the first refresh — that call will send If-None-Match and
        # get a cheap 304.
        self._cached_bundle: PolicyBundle | None = initial_bundle
        self._cached_etag: str | None = initial_etag
        # Serialize the (read cached_etag → HTTP → write cached_*) cycle.
        # Refresh runs on a to_thread worker, so two concurrent agent runs
        # sharing one source could otherwise interleave a write to
        # _cached_bundle with another's read of _cached_etag and pair the
        # bundle from one response with the etag from another → a later
        # spurious 200/304. The cost is serializing refreshes for shared
        # sources, which is fine: refresh is best-effort and rare-ish per
        # turn (most calls hit a cheap 304).
        self._lock = threading.Lock()

    def fetch(self) -> PolicyBundle | None:
        with self._lock:
            payload, etag = self._client.get_agent(
                self._agent_name, if_none_match=self._cached_etag
            )
            # 304 — nothing changed since last fetch. Cheap path.
            if payload is None:
                return self._cached_bundle

            bundle = decode_and_verify_platform_bundle(
                payload, self._client.public_key_bytes()
            )
            self._cached_bundle = bundle
            # Server-supplied ETag wins; fall back to wasm_hash for when the
            # response lacked an ETag header (older platform versions).
            self._cached_etag = etag or (
                f'"{bundle.wasm_hash}"'
                if bundle is not None and bundle.wasm_hash
                else None
            )
            return bundle


def decode_and_verify_platform_bundle(
    payload: dict, public_key_raw: bytes
) -> PolicyBundle | None:
    """Decode + verify the bundle in a platform :meth:`FortifyClient.get_agent`
    response.

    Returns ``None`` when the platform served no compiled bundle (the
    bundle fields are null — e.g. opa wasn't available on the control
    plane). Raises ``RuntimeError`` if a bundle WAS served but its
    signature or integrity check fails: a bad signature is never
    silently downgraded to the pydantic engine.
    """
    wasm_b64 = payload.get("bundle_wasm_b64")
    manifest_text = payload.get("bundle_manifest")
    sig_b64 = payload.get("bundle_signature_b64")
    if not wasm_b64 or not manifest_text or not sig_b64:
        return None

    try:
        wasm = base64.b64decode(wasm_b64)
        signature = base64.b64decode(sig_b64)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"platform served a bundle but its base64 is malformed: {exc}"
        ) from exc

    bundle = PolicyBundle.from_parts(
        wasm_bytes=wasm,
        manifest_bytes=manifest_text.encode("utf-8"),
        signature=signature,
    )
    try:
        bundle.verify_signature(public_key_raw)
        bundle.verify_integrity()
    except (BundleSignatureError, BundleIntegrityError) as exc:
        raise RuntimeError(
            f"platform-served policy bundle failed verification: {exc}. "
            "Refusing to run rather than silently downgrading to the "
            "pydantic engine."
        ) from exc
    return bundle


# ---------------------------------------------------------------------------
# Local sources — for FORTIFY_LOCAL_POLICY without a platform in the loop
# ---------------------------------------------------------------------------


class BundleDirPolicySource:
    """Refresh a pre-built bundle directory on every fetch via mtime.

    Wraps :meth:`PolicyBundle.from_disk` with two pieces of dev-loop polish:

      * ``fetch()`` only reloads when the bundle manifest's mtime has
        changed since the last load — so a quiet run pays one ``stat()``
        and reuses the cached :class:`PolicyBundle` instance (identity
        match → the agent runtime's refresh seam skips its swap).
      * Verification (``verify_integrity`` + an optional
        ``verify_signature``) runs on every reload, so a hand-edited
        wasm/manifest pair never slips through.

    Layout mirrors what ``fortify policy build`` emits — a directory
    containing ``{stem}.yaml``, ``{stem}.rego``, ``{stem}.wasm``,
    ``{stem}.bundle.json``, and optionally ``{stem}.bundle.json.sig``.
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        verify_with: bytes | None = None,
    ) -> None:
        self._directory = Path(directory)
        # When set, every reload's signature is verified against this raw
        # Ed25519 public key. None disables the check (still enforces
        # integrity — wasm matches the manifest's wasm_hash).
        self._verify_with = verify_with
        self._cached: PolicyBundle | None = None
        self._cached_mtime_ns: int | None = None
        # Same concurrency guard as PlatformPolicySource — protect the
        # (read cached_mtime → stat → maybe reload → write cached_*)
        # cycle so two concurrent fetches can't pair an old mtime with a
        # new bundle (or vice versa).
        self._lock = threading.Lock()

    def fetch(self) -> PolicyBundle | None:
        manifest_path = self._locate_manifest()
        try:
            mtime_ns = manifest_path.stat().st_mtime_ns
        except OSError as exc:
            raise RuntimeError(
                f"FORTIFY_LOCAL_POLICY bundle at {self._directory} disappeared: {exc}"
            ) from exc

        with self._lock:
            if self._cached is not None and mtime_ns == self._cached_mtime_ns:
                return self._cached

            try:
                bundle = PolicyBundle.from_disk(self._directory)
                bundle.verify_integrity()
            except (BundleLoadError, BundleIntegrityError) as exc:
                raise RuntimeError(
                    f"FORTIFY_LOCAL_POLICY bundle at {self._directory} failed to load: {exc}"
                ) from exc

            if self._verify_with is not None:
                try:
                    bundle.verify_signature(self._verify_with)
                except BundleSignatureError as exc:
                    raise RuntimeError(
                        f"FORTIFY_LOCAL_POLICY bundle at {self._directory} failed "
                        f"signature verification: {exc}"
                    ) from exc

            self._cached = bundle
            self._cached_mtime_ns = mtime_ns
            return bundle

    def _locate_manifest(self) -> Path:
        """Find the single ``*.bundle.json`` we're refreshing against.

        Same disambiguation rule as :meth:`PolicyBundle.from_disk`: one
        manifest per directory; refuse rather than guess if there are
        zero or multiple.
        """
        if not self._directory.is_dir():
            raise RuntimeError(
                f"FORTIFY_LOCAL_POLICY={self._directory} is not a directory."
            )
        manifests = sorted(self._directory.glob("*.bundle.json"))
        if not manifests:
            raise RuntimeError(
                f"FORTIFY_LOCAL_POLICY={self._directory}: no *.bundle.json found. "
                "Build with `fortify policy build` first."
            )
        if len(manifests) > 1:
            raise RuntimeError(
                f"FORTIFY_LOCAL_POLICY={self._directory}: multiple bundle "
                f"manifests {[m.name for m in manifests]} — pass an explicit "
                "directory containing one."
            )
        return manifests[0]


class YamlPolicySource:
    """Recompile a ``policy.yaml`` into a bundle whenever the file changes.

    Closes the dev iteration loop without a platform: edit the yaml,
    save, re-run the agent → the new policy is live. Behaves like
    :class:`PlatformPolicySource` from the runtime's perspective —
    cached when nothing's changed, fresh instance when it has.

    Recompilation shells out to ``opa`` via :func:`build_signed_bundle`
    (same path the platform uses at save time), so the produced
    :class:`PolicyBundle` is byte-for-byte what the platform would have
    served — minus the platform's signature, unless ``sign`` is supplied.

    The default unsigned mode is the happy path for dev. Production /
    CI that requires authenticity should run against the platform
    (PlatformPolicySource) or use a pre-built signed bundle directory
    (BundleDirPolicySource) — local yaml signing is mostly noise since
    the dev box holds the signing key.
    """

    def __init__(
        self,
        yaml_path: Path | str,
        *,
        sign: "Callable[[bytes], bytes] | None" = None,
        opa_bin: str | None = None,
    ) -> None:
        self._yaml_path = Path(yaml_path)
        self._sign = sign
        self._opa_bin = opa_bin
        self._cached: PolicyBundle | None = None
        self._cached_mtime_ns: int | None = None
        # Same concurrency guard as the other sources. Note: compiling
        # yaml under the lock means concurrent agent runs sharing this
        # source serialize on every recompile — that's fine, OPA
        # compilation is the slow step we already to_thread off the
        # event loop, and the unchanged-mtime cheap path is lock-free
        # in practice (the comparison itself is microseconds).
        self._lock = threading.Lock()

    def fetch(self) -> PolicyBundle | None:
        try:
            mtime_ns = self._yaml_path.stat().st_mtime_ns
        except OSError as exc:
            raise RuntimeError(
                f"FORTIFY_LOCAL_POLICY yaml at {self._yaml_path} disappeared: {exc}"
            ) from exc

        with self._lock:
            if self._cached is not None and mtime_ns == self._cached_mtime_ns:
                return self._cached

            try:
                yaml_text = self._yaml_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(
                    f"FORTIFY_LOCAL_POLICY yaml at {self._yaml_path} could not be "
                    f"read: {exc}"
                ) from exc

            try:
                built = build_signed_bundle(
                    yaml_text,
                    source_name=self._yaml_path.name,
                    sign=self._sign,
                    opa_bin=self._opa_bin,
                )
            except Exception as exc:  # opa missing, malformed yaml, bad constraints
                raise RuntimeError(
                    f"FORTIFY_LOCAL_POLICY yaml at {self._yaml_path} failed to "
                    f"compile: {exc}"
                ) from exc

            if built.wasm_bytes is None:
                # build_signed_bundle returns wasm_bytes=None only with compile_wasm=False;
                # we never set that, so this is paranoia for future-proofing.
                raise RuntimeError(
                    f"FORTIFY_LOCAL_POLICY yaml at {self._yaml_path} compiled "
                    "without wasm — refusing to enforce a non-wasm bundle."
                )

            bundle = PolicyBundle.from_parts(
                wasm_bytes=built.wasm_bytes,
                manifest_bytes=built.manifest_bytes,
                signature=built.signature,
            )
            # Integrity is trivially satisfied (we just produced both halves),
            # but the check catches bugs in build_signed_bundle and keeps the
            # invariant uniform across sources.
            try:
                bundle.verify_integrity()
            except BundleIntegrityError as exc:
                raise RuntimeError(
                    f"FORTIFY_LOCAL_POLICY yaml at {self._yaml_path}: freshly "
                    f"built bundle failed integrity: {exc}"
                ) from exc

            self._cached = bundle
            self._cached_mtime_ns = mtime_ns
            return bundle


# ---------------------------------------------------------------------------
# FORTIFY_LOCAL_POLICY — env var → PolicySource factory
#
# "Turn the local-override env var into a source" is source-construction
# logic, so it lives next to the source classes it instantiates. Moved here
# from fortify.agents.loader (policy-binding spec, phase 1); the loader
# re-imports these names for back-compat with existing callers and tests.
# ---------------------------------------------------------------------------

_LOCAL_POLICY_ENV_VAR = "FORTIFY_LOCAL_POLICY"
_BUNDLE_PUBKEY_ENV_VAR = "FORTIFY_BUNDLE_PUBKEY_PATH"
_REQUIRE_SIGNATURE_ENV_VAR = "FORTIFY_BUNDLE_REQUIRE_SIGNATURE"
_BUNDLE_SIGN_KEY_ENV_VAR = "FORTIFY_BUNDLE_SIGN_KEY_PATH"


def _truthy(value: str | None) -> bool:
    """Parse a boolean-ish env var ('1', 'true', 'yes' → True)."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_pubkey_for_verification(override_path: str) -> bytes | None:
    """Resolve ``FORTIFY_BUNDLE_PUBKEY_PATH`` into raw bytes for a local source.

    Returns ``None`` when no pubkey is configured AND the env doesn't
    require signatures. Raises when ``FORTIFY_BUNDLE_REQUIRE_SIGNATURE``
    is set but no key is provided (we'd have nothing to verify against).
    """
    require = _truthy(os.environ.get(_REQUIRE_SIGNATURE_ENV_VAR))
    pubkey_path = os.environ.get(_BUNDLE_PUBKEY_ENV_VAR)

    if not pubkey_path:
        if require:
            raise RuntimeError(
                f"{_REQUIRE_SIGNATURE_ENV_VAR} is set but "
                f"{_BUNDLE_PUBKEY_ENV_VAR} is unset — no key to verify the "
                f"bundle at {override_path!r} against."
            )
        return None

    try:
        return decode_key(Path(pubkey_path).read_text(encoding="utf-8").strip())
    except (OSError, SignatureError) as exc:
        raise RuntimeError(
            f"{_BUNDLE_PUBKEY_ENV_VAR}={pubkey_path!r} could not be read as a "
            f"base64url public key: {exc}"
        ) from exc


def _local_sign_callable() -> "Callable[[bytes], bytes] | None":
    """Build a sign callback from ``FORTIFY_BUNDLE_SIGN_KEY_PATH`` if set.

    Opt-in: the default :class:`YamlPolicySource` builds unsigned bundles
    (dev-loop default — signing locally with a key on the dev box adds
    no real authenticity). When set, the file is read as a base64url raw
    Ed25519 private key and used to sign every recompile, so the
    resulting bundle's ``is_signed`` flag matches what the platform
    would have produced. Useful when a downstream check requires
    ``is_signed`` to be true.
    """
    key_path = os.environ.get(_BUNDLE_SIGN_KEY_ENV_VAR)
    if not key_path:
        return None
    try:
        private_raw = decode_key(Path(key_path).read_text(encoding="utf-8").strip())
    except (OSError, SignatureError) as exc:
        raise RuntimeError(
            f"{_BUNDLE_SIGN_KEY_ENV_VAR}={key_path!r} could not be read as a "
            f"base64url private key: {exc}"
        ) from exc

    from fortify.security.signing import sign_bytes

    return lambda data: sign_bytes(data, private_raw)


def _local_policy_source() -> PolicySource | None:
    """Resolve ``$FORTIFY_LOCAL_POLICY`` into a :class:`PolicySource`, if set.

    Dispatch by path shape:

      * ``<dir>`` → :class:`BundleDirPolicySource` (pre-built bundle from
        ``fortify policy build``; mtime-refreshed).
      * ``*.yaml`` / ``*.yml`` → :class:`YamlPolicySource` (auto-compile
        on save).

    Signature handling preserves today's matrix via
    :func:`_resolve_pubkey_for_verification` + the
    ``FORTIFY_BUNDLE_REQUIRE_SIGNATURE`` knob — devs stay frictionless
    on unsigned bundles, CI opts into strictness.
    """
    override_path = os.environ.get(_LOCAL_POLICY_ENV_VAR)
    if not override_path:
        return None
    target = Path(override_path)

    if target.is_dir():
        verify_with = _resolve_pubkey_for_verification(override_path)
        return BundleDirPolicySource(target, verify_with=verify_with)
    if target.suffix in {".yaml", ".yml"} and target.is_file():
        # REQUIRE_SIGNATURE blocks the unsigned-yaml path unless the user
        # also configures a sign key. The check fires at fetch time via
        # _verify_local_source_signature_policy — keeps the surface uniform
        # with the dir path.
        return YamlPolicySource(target, sign=_local_sign_callable())
    raise RuntimeError(
        f"{_LOCAL_POLICY_ENV_VAR}={override_path!r}: expected a bundle "
        "directory (output of `fortify policy build`) or a .yaml file."
    )


def _verify_local_source_signature_policy(
    bundle: PolicyBundle, source: PolicySource
) -> None:
    """Apply ``FORTIFY_BUNDLE_REQUIRE_SIGNATURE`` to a freshly-fetched bundle.

    BundleDirPolicySource already verifies the signature against the
    configured pubkey when it loads, so the only thing left for that
    branch is the ``require=true & no signature & no key`` case (handled
    by _resolve_pubkey_for_verification at construction). For
    YamlPolicySource the rule is: REQUIRE_SIGNATURE=true & sign-key
    unset → refuse, because we know the bundle is unsigned and there's
    nothing to verify.
    """
    if not _truthy(os.environ.get(_REQUIRE_SIGNATURE_ENV_VAR)):
        return
    if isinstance(source, YamlPolicySource) and not bundle.is_signed:
        raise RuntimeError(
            f"{_REQUIRE_SIGNATURE_ENV_VAR} is set but {_LOCAL_POLICY_ENV_VAR} "
            "points at an unsigned yaml source. Set "
            f"{_BUNDLE_SIGN_KEY_ENV_VAR} to a base64url private key, or use a "
            "pre-built signed bundle directory instead."
        )


def _announce_local_override(
    bundle: PolicyBundle, source: PolicySource, override_path: str
) -> None:
    """Loud stderr line so devs notice when the local override is active."""
    import sys

    short = bundle.wasm_hash[:12] if bundle.wasm_hash else "?"
    signed = "signed" if bundle.is_signed else "unsigned"
    kind = "yaml" if isinstance(source, YamlPolicySource) else "bundle-dir"
    print(
        f"[fortify] {_LOCAL_POLICY_ENV_VAR} active ({kind}): "
        f"{override_path} (wasm_hash={short}, {signed})",
        file=sys.stderr,
    )


def _local_policy_override() -> tuple[PolicyBundle, PolicySource] | None:
    """Resolve ``$FORTIFY_LOCAL_POLICY`` into a (bundle, source) pair.

    Returns ``None`` when the env var is unset. The bundle is the
    initial enforcement policy (ready to hand off to ``enforce_policy``);
    the source is attached to the agent so per-run refresh picks up
    yaml edits / bundle rebuilds without a restart.

    Failures (missing file, bad signature, opa not on PATH for a yaml
    source) raise loudly — silently degrading a security override
    would defeat the point.
    """
    source = _local_policy_source()
    if source is None:
        return None
    bundle = source.fetch()
    if bundle is None:
        # Local sources only return None for "no bundle configured" — an
        # impossible state here since we'd have returned None above.
        raise RuntimeError(
            f"{_LOCAL_POLICY_ENV_VAR}: source produced no bundle (internal "
            "invariant violated)."
        )
    _verify_local_source_signature_policy(bundle, source)
    _announce_local_override(bundle, source, os.environ[_LOCAL_POLICY_ENV_VAR])
    return bundle, source
