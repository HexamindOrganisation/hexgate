"""Policy sources — abstractions over "where the current policy lives."

The runtime fetches a :class:`~hexgate.security.decision.PolicyEngine`
(or ``None``) from a source at every agent run; the source decides
whether that's cheap or not. Three implementations cover the production
+ local-dev workflows:

  * :class:`PlatformPolicySource` — HTTP fetch with ``If-None-Match`` /
    ``304 Not Modified``, so unchanged bundles cost one tiny round trip
    instead of a full payload + signature verify + wasm re-instantiation.
    Falls back to the pydantic engine (a :class:`PolicySet` derived from
    the response's ``policy_yaml``) when the platform served no compiled
    bundle — the typical Modal / no-opa demo deployment shape.
  * :class:`BundleDirPolicySource` — refresh a pre-built bundle directory
    on disk (today's ``HEXGATE_LOCAL_POLICY=<dir>`` path, made mtime-aware
    so a rebuild via ``hexgate policy build`` takes effect on the next run).
  * :class:`YamlPolicySource` — auto-recompile a ``policy.yaml`` when its
    mtime changes. The dev edits → saves → runs loop matches the platform's
    hot-reload UX without the platform in the loop.

The common interface is :class:`PolicySource`; the agent runtime depends
on the protocol, not on any concrete type.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import yaml

from hexgate.security.bundle import (
    BundleIntegrityError,
    BundleLoadError,
    BundleSignatureError,
    PolicyBundle,
    build_signed_bundle,
)
from hexgate.security.decision import PolicyEngine
from hexgate.security.policy_set import PolicySetError, load_policy_set_from_dict
from hexgate.security.signing import SignatureError, decode_key

if TYPE_CHECKING:
    from collections.abc import Callable

    from hexgate.cloud.client import HexgateClient


logger = logging.getLogger("hexgate.security.source")


class PolicyContentError(RuntimeError):
    """Platform served a payload, but the policy content is invalid.

    Distinct from transient errors (network, signature) so
    :meth:`PolicyBinding.refresh` can log at ``error`` level —
    dashboard-saved-but-runtime-rejected is a correctness drift, not
    "retry later".
    """


class PolicySource(Protocol):
    """Produces a current :class:`PolicyEngine` (or ``None``) on demand.

    Implementations are expected to be **cheap when nothing has changed**
    — caching, ETags, or mtime checks — so the agent runtime can call
    :meth:`fetch` at the top of every run without measurable cost.

    A returned ``None`` means "no engine is configured for this source"
    (e.g. the platform served no policy at all). Callers keep whatever
    engine they had before. The runtime's :class:`PolicyBinding.refresh`
    relies on this: it only swaps when ``fetch()`` returns something
    distinct from the current engine.
    """

    def fetch(self) -> PolicyEngine | None: ...


class PlatformPolicySource:
    """Pull a current policy engine from the platform, with ETag/304.

    Two engines flow out, depending on what the platform has compiled:

      * **WASM bundle** (production shape) — when the platform's
        ``compiled_wasm`` is populated, we get a signed bundle back and
        return a verified :class:`PolicyBundle`. ETag = ``wasm_hash``;
        unchanged bundles hit ``304`` and re-use the cached object.
      * **Pydantic fallback** (no-opa / demo shape) — when the platform
        couldn't compile (no ``opa`` on the control plane), the response
        carries ``policy_yaml`` but null bundle fields. We hash the yaml,
        compare against the last seen hash, and re-construct a fresh
        :class:`PolicySet` only when the yaml content actually changed.

    Without the pydantic-fallback branch a policy edit would silently
    no-op for any deployment without opa — :meth:`fetch` would always
    return ``None`` (no bundle), :class:`PolicyBinding.refresh` would
    treat that as "nothing served" and skip the swap, and the initial
    engine built by :func:`platform_policy_from_payload` would stay
    frozen forever.

    Verification fails on the bundle path are fatal (a tampered bundle
    is never silently downgraded). Verification uses the same public
    key the SDK already trusts for biscuit verification.
    """

    def __init__(
        self,
        client: HexgateClient,
        agent_name: str,
        *,
        initial_bundle: PolicyBundle | None = None,
        initial_etag: str | None = None,
        initial_engine: PolicyEngine | None = None,
        initial_yaml_hash: str | None = None,
    ) -> None:
        self._client = client
        self._agent_name = agent_name
        # Pre-seed when the caller already fetched + verified at load time.
        # `initial_engine` covers both shapes (a PolicyBundle on the WASM
        # path, a PolicySet on the pydantic-fallback path); `initial_bundle`
        # stays as a back-compat alias that callers used before we
        # broadened the engine type.
        self._cached_engine: PolicyEngine | None = (
            initial_engine if initial_engine is not None else initial_bundle
        )
        self._cached_etag: str | None = initial_etag
        # Hash of the `policy_yaml` text that produced the cached *pydantic*
        # engine. Used solely on the no-bundle branch to decide whether
        # the platform's response represents a real change: a same-hash
        # response returns the cached PolicySet (preserves identity → the
        # binding's `is policy` check skips the swap); a new hash builds
        # and caches a fresh one. ``None`` on the bundle path (we use
        # ``_cached_etag`` for that).
        self._cached_yaml_hash: str | None = initial_yaml_hash
        # Serialize the (read cached_etag → HTTP → write cached_*) cycle.
        # Refresh runs on a to_thread worker, so two concurrent agent runs
        # sharing one source could otherwise interleave a write to
        # _cached_engine with another's read of _cached_etag and pair the
        # bundle from one response with the etag from another → a later
        # spurious 200/304. The cost is serializing refreshes for shared
        # sources, which is fine: refresh is best-effort and rare-ish per
        # turn (most calls hit a cheap 304).
        self._lock = threading.Lock()

    def fetch(self) -> PolicyEngine | None:
        with self._lock:
            payload, etag = self._client.get_agent(
                self._agent_name, if_none_match=self._cached_etag
            )
            # 304 — nothing changed since last fetch. Cheap path.
            if payload is None:
                return self._cached_engine

            bundle = decode_and_verify_platform_bundle(
                payload, self._client.public_key_bytes()
            )
            if bundle is not None:
                # WASM path. ETag tracking is on the wasm_hash; the yaml
                # hash is irrelevant here, clear it so a later transition
                # to the pydantic branch (platform loses opa) doesn't
                # mistakenly reuse a stale hash from the old wasm world.
                self._cached_engine = bundle
                self._cached_etag = etag or (
                    f'"{bundle.wasm_hash}"' if bundle.wasm_hash else None
                )
                self._cached_yaml_hash = None
                return bundle

            # No bundle — platform couldn't compile (no opa, etc.) but
            # served the raw policy_yaml. Build a PolicySet from it.

            # (#1) Refuse the downgrade under strict mode. Load-time
            # already refuses; this catches opa-went-down mid-session.
            # Caught by binding.refresh → keeps last verified bundle.
            if _truthy(os.environ.get(_REQUIRE_SIGNATURE_ENV_VAR)):
                raise RuntimeError(
                    f"{_REQUIRE_SIGNATURE_ENV_VAR} is set but no signed "
                    f"bundle served for {self._agent_name!r} on refresh — "
                    "keeping last verified policy."
                )

            # (#3) Ignore server ETag on this branch — its semantics
            # aren't defined here, and a 304 would skip the hash check
            # below and swallow an edit. Yaml-hash is the change detector.
            yaml_text = payload.get("policy_yaml") or ""
            new_hash = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()
            if new_hash == self._cached_yaml_hash and self._cached_engine is not None:
                # Identity preserved → binding's `is` check skips the swap.
                self._cached_etag = None
                return self._cached_engine

            # (#2) Surface parse/validate failures as PolicyContentError
            # so binding logs at ERROR — silent swallow would recreate
            # the original bug for invalid edits.
            try:
                parsed = yaml.safe_load(yaml_text) or {}
            except yaml.YAMLError as exc:
                raise PolicyContentError(
                    f"unparseable policy_yaml for {self._agent_name!r}: {exc}"
                ) from exc
            try:
                new_engine = load_policy_set_from_dict(parsed)
            except (PolicySetError, ValueError, TypeError) as exc:
                # ValueError covers pydantic ValidationError too.
                raise PolicyContentError(
                    f"invalid policy_yaml for {self._agent_name!r}: {exc}"
                ) from exc

            # (#4) Per-turn cost = full GET + sha256; parse only on change.
            # Can't 304 without server ETag-on-policy_yaml (future fix).
            # Until then the per-turn cost is one round trip + a sha256,
            # acceptable for the demo-shaped deployments this branch
            # targets.
            self._cached_engine = new_engine
            self._cached_yaml_hash = new_hash
            self._cached_etag = None
            return self._cached_engine


def decode_and_verify_platform_bundle(
    payload: dict, public_key_raw: bytes
) -> PolicyBundle | None:
    """Decode + verify the bundle in a platform :meth:`HexgateClient.get_agent`
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
# Local sources — for HEXGATE_LOCAL_POLICY without a platform in the loop
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

    Layout mirrors what ``hexgate policy build`` emits — a directory
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
                f"HEXGATE_LOCAL_POLICY bundle at {self._directory} disappeared: {exc}"
            ) from exc

        with self._lock:
            if self._cached is not None and mtime_ns == self._cached_mtime_ns:
                return self._cached

            try:
                bundle = PolicyBundle.from_disk(self._directory)
                bundle.verify_integrity()
            except (BundleLoadError, BundleIntegrityError) as exc:
                raise RuntimeError(
                    f"HEXGATE_LOCAL_POLICY bundle at {self._directory} failed to load: {exc}"
                ) from exc

            if self._verify_with is not None:
                try:
                    bundle.verify_signature(self._verify_with)
                except BundleSignatureError as exc:
                    raise RuntimeError(
                        f"HEXGATE_LOCAL_POLICY bundle at {self._directory} failed "
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
                f"HEXGATE_LOCAL_POLICY={self._directory} is not a directory."
            )
        manifests = sorted(self._directory.glob("*.bundle.json"))
        if not manifests:
            raise RuntimeError(
                f"HEXGATE_LOCAL_POLICY={self._directory}: no *.bundle.json found. "
                "Build with `hexgate policy build` first."
            )
        if len(manifests) > 1:
            raise RuntimeError(
                f"HEXGATE_LOCAL_POLICY={self._directory}: multiple bundle "
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
        sign: Callable[[bytes], bytes] | None = None,
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
                f"HEXGATE_LOCAL_POLICY yaml at {self._yaml_path} disappeared: {exc}"
            ) from exc

        with self._lock:
            if self._cached is not None and mtime_ns == self._cached_mtime_ns:
                return self._cached

            try:
                yaml_text = self._yaml_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(
                    f"HEXGATE_LOCAL_POLICY yaml at {self._yaml_path} could not be "
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
                    f"HEXGATE_LOCAL_POLICY yaml at {self._yaml_path} failed to "
                    f"compile: {exc}"
                ) from exc

            if built.wasm_bytes is None:
                # build_signed_bundle returns wasm_bytes=None only with compile_wasm=False;
                # we never set that, so this is paranoia for future-proofing.
                raise RuntimeError(
                    f"HEXGATE_LOCAL_POLICY yaml at {self._yaml_path} compiled "
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
                    f"HEXGATE_LOCAL_POLICY yaml at {self._yaml_path}: freshly "
                    f"built bundle failed integrity: {exc}"
                ) from exc

            self._cached = bundle
            self._cached_mtime_ns = mtime_ns
            return bundle


# ---------------------------------------------------------------------------
# HEXGATE_LOCAL_POLICY — env var → PolicySource factory. Single source of
# truth for the REQUIRE_SIGNATURE matrix (SignaturePolicy), used by both the
# binding path (resolve_policy) and the loaders. hexgate.agents.loader
# re-imports _local_policy_override from here for back-compat.
# ---------------------------------------------------------------------------

_LOCAL_POLICY_ENV_VAR = "HEXGATE_LOCAL_POLICY"
_BUNDLE_PUBKEY_ENV_VAR = "HEXGATE_BUNDLE_PUBKEY_PATH"
_REQUIRE_SIGNATURE_ENV_VAR = "HEXGATE_BUNDLE_REQUIRE_SIGNATURE"
_BUNDLE_SIGN_KEY_ENV_VAR = "HEXGATE_BUNDLE_SIGN_KEY_PATH"


def _truthy(value: str | None) -> bool:
    """Parse a boolean-ish env var ('1', 'true', 'yes' → True)."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class SignaturePolicy:
    """The (pubkey, require_signature) pair resolved once from env.

    Concentrates every cell of the ``HEXGATE_BUNDLE_REQUIRE_SIGNATURE``
    × ``HEXGATE_BUNDLE_PUBKEY_PATH`` × (yaml | bundle-dir) matrix into
    one place so the safety story is auditable from a single file.

    Matrix:
      * require=true  + no pubkey               → :meth:`from_env` raises
        (no key to verify against — refuse to start).
      * require=true  + yaml + unsigned bundle  → :meth:`check_yaml_bundle`
        raises at fetch time (yaml produces unsigned by default; refuse to
        enforce when strict mode demands authenticity).
      * require=false + signed bundle + pubkey  → BundleDir verifies via
        ``verify_with`` on every reload.
      * require=false + signed bundle + no key  → :meth:`warn_if_unverified`
        emits a heads-up at announce time. Dev sees "signed" + warning;
        no enforcement happens.
      * require=false + unsigned                → silent OK.
    """

    def __init__(self, *, verify_with: bytes | None, require_signature: bool) -> None:
        self.verify_with = verify_with
        self.require_signature = require_signature

    @classmethod
    def from_env(cls, override_path: str) -> SignaturePolicy:
        """Build the policy from env vars, raising on require-without-key.

        ``override_path`` is the value of ``HEXGATE_LOCAL_POLICY`` — used
        only for error messages so the operator sees which load is
        being refused. Called unconditionally (before path-shape
        dispatch), so ``require=true`` + no pubkey fails fast for both
        the yaml and bundle-dir paths.
        """
        require = _truthy(os.environ.get(_REQUIRE_SIGNATURE_ENV_VAR))
        pubkey_path = os.environ.get(_BUNDLE_PUBKEY_ENV_VAR)

        if not pubkey_path:
            if require:
                raise RuntimeError(
                    f"{_REQUIRE_SIGNATURE_ENV_VAR} is set but "
                    f"{_BUNDLE_PUBKEY_ENV_VAR} is unset — no key to verify "
                    f"the bundle at {override_path!r} against."
                )
            return cls(verify_with=None, require_signature=False)

        try:
            verify_with = decode_key(
                Path(pubkey_path).read_text(encoding="utf-8").strip()
            )
        except (OSError, SignatureError) as exc:
            raise RuntimeError(
                f"{_BUNDLE_PUBKEY_ENV_VAR}={pubkey_path!r} could not be read "
                f"as a base64url public key: {exc}"
            ) from exc
        return cls(verify_with=verify_with, require_signature=require)

    def check_yaml_bundle(self, bundle: PolicyBundle, yaml_path: str) -> None:
        """At fetch time, refuse an unsigned yaml-built bundle under strict mode.

        ``BundleDirPolicySource`` is already covered: its constructor
        receives :attr:`verify_with` and verifies on every reload. The
        yaml branch has nothing for BundleDir to verify against (yaml
        sources build their bundles locally) — so strict mode means
        either sign locally via ``HEXGATE_BUNDLE_SIGN_KEY_PATH`` or
        switch to a pre-built signed bundle dir.
        """
        if self.require_signature and not bundle.is_signed:
            raise RuntimeError(
                f"{_REQUIRE_SIGNATURE_ENV_VAR} is set but "
                f"{_LOCAL_POLICY_ENV_VAR}={yaml_path!r} points at an "
                f"unsigned yaml source. Set {_BUNDLE_SIGN_KEY_ENV_VAR} to "
                "sign locally, or switch to a pre-built signed bundle dir."
            )

    def warn_if_unverified(self, bundle: PolicyBundle) -> None:
        """Emit a heads-up when a signed bundle loads without a pubkey.

        Permissive-mode only — strict mode would already have raised in
        :meth:`from_env`. Without the warning the dev sees "signed" in
        the announce line and reasonably assumes authenticity was
        checked, when it wasn't. Verification behaviour is unchanged
        (we never verified there); only the heads-up is restored.
        """
        if bundle.is_signed and self.verify_with is None:
            import sys

            print(
                f"[hexgate] warning: override bundle is signed but "
                f"{_BUNDLE_PUBKEY_ENV_VAR} is unset — signature NOT verified.",
                file=sys.stderr,
            )


def _local_sign_callable() -> Callable[[bytes], bytes] | None:
    """Build a sign callback from ``HEXGATE_BUNDLE_SIGN_KEY_PATH`` if set.

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

    from hexgate.security.signing import sign_bytes

    return lambda data: sign_bytes(data, private_raw)


def _local_policy_source(sig_policy: SignaturePolicy) -> PolicySource | None:
    """Resolve ``$HEXGATE_LOCAL_POLICY`` into a :class:`PolicySource`, if set.

    Dispatch by path shape:

      * ``<dir>`` → :class:`BundleDirPolicySource` (pre-built bundle from
        ``hexgate policy build``; mtime-refreshed). Its ``verify_with``
        comes from ``sig_policy.verify_with``.
      * ``*.yaml`` / ``*.yml`` → :class:`YamlPolicySource` (auto-compile
        on save). Strict-mode signing is checked at fetch time via
        ``sig_policy.check_yaml_bundle``.

    The full ``REQUIRE_SIGNATURE`` matrix lives on :class:`SignaturePolicy`
    — see its docstring for the cell-by-cell table.
    """
    override_path = os.environ.get(_LOCAL_POLICY_ENV_VAR)
    if not override_path:
        return None
    target = Path(override_path)

    if target.is_dir():
        return BundleDirPolicySource(target, verify_with=sig_policy.verify_with)
    if target.suffix in {".yaml", ".yml"} and target.is_file():
        return YamlPolicySource(target, sign=_local_sign_callable())
    raise RuntimeError(
        f"{_LOCAL_POLICY_ENV_VAR}={override_path!r}: expected a bundle "
        "directory (output of `hexgate policy build`) or a .yaml file."
    )


def _announce_local_override(
    bundle: PolicyBundle, source: PolicySource, override_path: str
) -> None:
    """Loud stderr line so devs notice when the local override is active.

    Signed-but-unverified warnings live on
    :meth:`SignaturePolicy.warn_if_unverified` and fire from
    :func:`_local_policy_override` — this function is purely the
    "what got loaded" announce line.
    """
    import sys

    short = bundle.wasm_hash[:12] if bundle.wasm_hash else "?"
    signed = "signed" if bundle.is_signed else "unsigned"
    kind = "yaml" if isinstance(source, YamlPolicySource) else "bundle-dir"
    print(
        f"[hexgate] {_LOCAL_POLICY_ENV_VAR} active ({kind}): "
        f"{override_path} (wasm_hash={short}, {signed})",
        file=sys.stderr,
    )


def _local_policy_override() -> tuple[PolicyBundle, PolicySource] | None:
    """Resolve ``$HEXGATE_LOCAL_POLICY`` into a (bundle, source) pair.

    Returns ``None`` when the env var is unset. The bundle is the
    initial enforcement policy (ready to hand off to ``enforce_policy``);
    the source is attached to the agent so per-run refresh picks up
    yaml edits / bundle rebuilds without a restart.

    Failures (missing file, bad signature, opa not on PATH for a yaml
    source) raise loudly — silently degrading a security override
    would defeat the point. Signature-policy enforcement (the
    ``REQUIRE_SIGNATURE`` matrix) is centralised on :class:`SignaturePolicy`.
    """
    override_path = os.environ.get(_LOCAL_POLICY_ENV_VAR)
    if not override_path:
        return None
    # Build the signature policy once at startup. ``from_env`` raises
    # immediately if require-signature is set without a pubkey — fail
    # fast, before any agent code runs, on both the yaml and dir paths.
    sig_policy = SignaturePolicy.from_env(override_path)
    source = _local_policy_source(sig_policy)
    if source is None:
        # Defensive: we already null-checked override_path above; if
        # _local_policy_source returns None here it'd be an internal
        # invariant violation.
        return None
    bundle = source.fetch()
    if bundle is None:
        # Local sources only return None for "no bundle configured" — an
        # impossible state here since we'd have returned None above.
        raise RuntimeError(
            f"{_LOCAL_POLICY_ENV_VAR}: source produced no bundle (internal "
            "invariant violated)."
        )
    # YamlPolicySource builds unsigned bundles unless HEXGATE_BUNDLE_SIGN_KEY_PATH
    # is set; strict mode refuses those. BundleDir's own constructor already
    # gated on verify_with, so this is a no-op for the dir path.
    if isinstance(source, YamlPolicySource):
        sig_policy.check_yaml_bundle(bundle, override_path)
    _announce_local_override(bundle, source, override_path)
    sig_policy.warn_if_unverified(bundle)
    return bundle, source
