"""Policy sources — abstractions over "where the current policy lives."

The runtime fetches a :class:`PolicyBundle` (or ``None``) from a source at
every agent run; the source decides whether that's cheap or not. Today's
implementation:

  * :class:`PlatformPolicySource` — HTTP fetch with ``If-None-Match`` /
    ``304 Not Modified``, so unchanged bundles cost one tiny round trip
    instead of a full payload + signature verify + wasm re-instantiation.

Phase 8b will add:

  * ``BundleDirPolicySource`` — refresh a bundle directory on disk
    (today's ``FORTIFY_LOCAL_POLICY=<dir>`` path, made stat-aware).
  * ``YamlPolicySource`` — auto-recompile + re-sign a ``policy.yaml`` when
    its mtime changes, so dev iteration matches the platform's hot-reload
    UX without the platform in the loop.

The common interface is :class:`PolicySource`; the agent runtime depends
on the protocol, not on either concrete type.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Protocol

from fortify.security.bundle import (
    BundleIntegrityError,
    BundleSignatureError,
    PolicyBundle,
)

if TYPE_CHECKING:
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

    def fetch(self) -> PolicyBundle | None:
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
