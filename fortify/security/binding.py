"""Policy binding — hold an enforcer, keep it current from a source.

Tools close over the enforcer, so :meth:`PolicyBinding.refresh` (called
at every run boundary) hot-swaps ``enforcer.policy`` for all of them.
Resolve is fail-loud, refresh is fail-soft (stale, never unverified).
A platform 404 propagates; callers register and resolve again. No
implicit allow-all — static engines go through the plain constructor.
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
    """A policy binding could not be resolved (construction only)."""


class PolicyBinding:
    """An enforcer plus the optional source that keeps it current.

    Role still resolves per tool call from the :class:`~fortify.runtime.User`
    contextvar, so one binding serves many concurrent users.
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

        Precedence: ``FORTIFY_LOCAL_POLICY`` override → platform
        (``client`` or ``api_key``/``FORTIFY_KEY``) → raise. Eager and
        fail-loud; a platform 404 propagates as ``FortifyError``.
        """
        if not agent_name:
            raise PolicyBindingError(
                "PolicyBinding.resolve() requires a non-empty agent name — "
                "it is the platform lookup key."
            )

        override = _local_policy_override()
        if override is not None:
            bundle, source = override
            return cls(PolicyEnforcer(bundle, agent_name=agent_name), source)

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
        """Pull the current policy and swap it in; no-op without a source.

        Fail-soft: fetch/verification failures log a warning and keep
        the previous verified policy.
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
            return  # nothing served, or same cached object (304)
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

    Signed bundle → WASM engine; bundle-less → pydantic engine on
    ``policy_yaml`` (forbidden under ``FORTIFY_BUNDLE_REQUIRE_SIGNATURE``).
    Shared by :meth:`PolicyBinding.resolve` and ``load_fortify_agent``.
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

    # Pre-seeded so the next refresh is a 304 unless the policy changed.
    source = PlatformPolicySource(
        client, agent_name, initial_bundle=bundle, initial_etag=etag
    )
    return policy, source
