"""Policy binding — hold an enforcer, keep it current from a source.

A :class:`PolicyBinding` pairs the :class:`PolicyEnforcer` an agent's
wrapped tools close over with the :class:`PolicySource` that keeps it
current. Construction surfaces resolve a binding once and call
:meth:`PolicyBinding.refresh` at the top of every run; because tools hold
the *enforcer* and refresh rebinds ``enforcer.policy`` in place, one
binding keeps every previously wrapped tool current forever.

Two ways in:

  * :meth:`PolicyBinding.resolve` — the governed path: ``FORTIFY_LOCAL_POLICY``
    override → platform-verified bundle → raise. Fail-loud by design.
  * the plain constructor — the explicit static path:
    ``PolicyBinding(PolicyEnforcer(my_engine, agent_name=...))``. No source,
    refresh is a no-op. There is deliberately no implicit allow-all;
    ungoverned operation must be written down at the call site.

Failure semantics: **resolve is fail-loud, refresh is fail-soft** — a
transient network blip at refresh keeps the previous verified policy in
force (fail-open to staleness, never to no-policy), while a bundle that
fails verification can never install itself on either path.

Registration is not this module's job: a platform 404 propagates as
:class:`~fortify.cloud.client.FortifyError` (``status == 404``); callers
that want register-on-miss catch it, register via
:func:`fortify.cli.register.register_agent` (which builds a real manifest
from the actual agent object), and resolve again.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

import yaml

from fortify.security.enforcer import PolicyEnforcer
from fortify.security.policy_set import load_policy_set_from_dict
from fortify.security.source import (
    _LOCAL_POLICY_ENV_VAR,
    _REQUIRE_SIGNATURE_ENV_VAR,
    PlatformPolicySource,
    PolicySource,
    _local_policy_override,
    _truthy,
    decode_and_verify_platform_bundle,
)

if TYPE_CHECKING:
    from fortify.cloud.client import FortifyClient
    from fortify.security.decision import PolicyEngine

logger = logging.getLogger("fortify.security.binding")


class PolicyBindingError(RuntimeError):
    """A policy binding could not be resolved.

    Raised at construction only — refresh failures are logged and
    swallowed (the previous verified policy stays in force).
    """


class PolicyBinding:
    """An enforcer plus the optional source that keeps it current.

    The enforcer is the stable object every wrapped tool closes over;
    :meth:`refresh` rebinds ``enforcer.policy`` in place. Role resolution
    stays per-tool-call (the enforcer re-reads the
    :class:`~fortify.runtime.User` contextvar), so one binding safely
    serves many concurrent users.
    """

    def __init__(
        self, enforcer: PolicyEnforcer, source: PolicySource | None = None
    ) -> None:
        self.enforcer = enforcer
        self.source = source

    @classmethod
    def resolve(
        cls,
        agent_name: str,
        *,
        api_key: str | None = None,
        client: "FortifyClient | None" = None,
    ) -> "PolicyBinding":
        """Resolve the current policy for ``agent_name`` and bind it.

        Precedence: ``FORTIFY_LOCAL_POLICY`` override → platform (via
        ``client``, or a fresh one from ``api_key``/``FORTIFY_KEY``) →
        raise :class:`PolicyBindingError`.

        Eager by design: the fetch and all verification happen here, so
        failures are loud at construction and the enforcer always holds a
        real, verified policy before any run. A platform 404 propagates
        as :class:`~fortify.cloud.client.FortifyError` — register the
        agent and resolve again.
        """
        if not agent_name:
            raise PolicyBindingError(
                "PolicyBinding.resolve() requires a non-empty agent name — "
                "it is the platform lookup key."
            )

        # 1. Local override wins outright; the platform is not contacted.
        override = _local_policy_override()
        if override is not None:
            bundle, source = override
            return cls(PolicyEnforcer(bundle, agent_name=agent_name), source)

        # 2. Platform — when any credential is in play.
        if client is None and (api_key or os.environ.get("FORTIFY_KEY")):
            from fortify.cloud.client import FortifyClient, FortifyConfig

            client = FortifyClient(FortifyConfig.from_env(api_key=api_key))
        if client is not None:
            payload, etag = client.get_agent(agent_name)
            assert payload is not None, (
                "get_agent without If-None-Match — 304 impossible"
            )
            policy, source = platform_policy_from_payload(
                client, agent_name, payload, etag
            )
            return cls(PolicyEnforcer(policy, agent_name=agent_name), source)

        raise PolicyBindingError(
            f"no policy available for agent {agent_name!r}: FORTIFY_KEY is "
            f"not set and {_LOCAL_POLICY_ENV_VAR} is not set. Set a "
            "credential, point the override at a policy, or construct "
            "PolicyBinding(PolicyEnforcer(engine)) explicitly."
        )

    def refresh(self) -> None:
        """Pull the current policy and swap it in. Cheap when nothing changed.

        No-op when no source is attached (static policy). The
        :class:`PlatformPolicySource` does the ETag/304 dance — and owns
        its own lock — so the unchanged case costs one small HTTP round
        trip and the identity check below skips the swap entirely.

        Fail-soft: any fetch failure (network, or a served bundle that
        fails verification inside the source) logs a warning and keeps
        the previous verified policy in force. A tampered refresh can
        deny freshness; it can never install itself.
        """
        if self.source is None:
            return
        try:
            new_policy = self.source.fetch()
        except Exception as exc:  # noqa: BLE001 — refresh must not crash a run
            logger.warning(
                "policy refresh for agent %r failed: %s — keeping "
                "previously loaded policy",
                getattr(self.enforcer, "agent_name", "?"),
                exc,
            )
            return
        if new_policy is None or new_policy is self.enforcer.policy:
            # Source has nothing to offer, or the same object came back
            # (PlatformPolicySource returns the cached bundle on 304).
            return
        self.enforcer.policy = new_policy

    async def refresh_async(self) -> None:
        """Async entry point — runs :meth:`refresh` off the event loop."""
        await asyncio.to_thread(self.refresh)


def platform_policy_from_payload(
    client: "FortifyClient",
    agent_name: str,
    payload: dict,
    etag: str | None,
) -> tuple["PolicyEngine", PolicySource]:
    """Decode + verify a ``get_agent`` payload into ``(engine, seeded source)``.

    The signed WASM bundle is the primary engine; a bundle-less payload
    (e.g. opa missing on the control plane) falls back to the pydantic
    engine on the served ``policy_yaml`` — unless
    ``FORTIFY_BUNDLE_REQUIRE_SIGNATURE`` forbids it. Verification
    failures raise inside ``decode_and_verify`` — never downgraded.

    Shared by :meth:`PolicyBinding.resolve` (which fetches the payload
    itself) and ``load_fortify_agent`` (which already fetched it for the
    agent's YAMLs) — so the engine-selection rules live in one place and
    both paths cost a single round trip.
    """
    bundle = decode_and_verify_platform_bundle(payload, client.public_key_bytes())
    policy: "PolicyEngine"
    if bundle is not None:
        policy = bundle
    elif _truthy(os.environ.get(_REQUIRE_SIGNATURE_ENV_VAR)):
        raise PolicyBindingError(
            f"{_REQUIRE_SIGNATURE_ENV_VAR} is set but the platform served "
            f"no signed bundle for agent {agent_name!r} — the policy may "
            "not have compiled (is opa available on the control plane?). "
            "Refusing to fall back to the pydantic engine."
        )
    else:
        policy = load_policy_set_from_dict(
            yaml.safe_load(payload.get("policy_yaml") or "") or {}
        )

    # Pre-seed the source with what we just fetched + verified so the
    # next refresh is a 304 unless the policy changed.
    source = PlatformPolicySource(
        client, agent_name, initial_bundle=bundle, initial_etag=etag
    )
    return policy, source
