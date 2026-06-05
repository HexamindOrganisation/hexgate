"""Load packaged and local agent definitions from disk."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, TypeAlias

import yaml

from fortify.agents.factory import (
    AgentGraph,
    ApprovalHandler,
    create_agent,
    enforce_policy,
)
from fortify.agents.models import AgentSpec
from fortify.cloud.client import FortifyClient, FortifyConfig
from fortify.security import AgentPolicy, PolicyBundle, load_policy
from fortify.security.signing import SignatureError, decode_key

# NOTE(merge): fortify.security.source carries its own copy of the
# FORTIFY_LOCAL_POLICY resolution helpers (policy-binding spec, phase 1 —
# PolicyBinding.resolve goes through those). This module keeps the
# SignaturePolicy-based variants below, which the loader paths and
# tests/security/test_local_sources.py use. Consolidating the two copies
# onto SignaturePolicy is a follow-up on the policy-binding branch.
from fortify.security.source import (
    BundleDirPolicySource,
    PolicySource,
    YamlPolicySource,
)
from fortify.tools import (
    bash,
    edit_file,
    fetch,
    glob,
    grep,
    read_file,
    refund_order,
    web_search,
    write_file,
)
from fortify.tracing.langfuse import CallbackHandler

BUILTIN_TOOLS = {
    "bash": bash,
    "edit_file": edit_file,
    "fetch": fetch,
    "glob": glob,
    "grep": grep,
    "read_file": read_file,
    "refund_order": refund_order,
    "web_search": web_search,
    "write_file": write_file,
}
AgentSource = Literal["builtin", "local", "registered"]
AgentFactory: TypeAlias = Callable[..., tuple[AgentGraph, CallbackHandler]]
REGISTERED_AGENTS: dict[str, AgentFactory] = {}


_LOCAL_POLICY_ENV_VAR = "FORTIFY_LOCAL_POLICY"
_BUNDLE_PUBKEY_ENV_VAR = "FORTIFY_BUNDLE_PUBKEY_PATH"
_REQUIRE_SIGNATURE_ENV_VAR = "FORTIFY_BUNDLE_REQUIRE_SIGNATURE"
_BUNDLE_SIGN_KEY_ENV_VAR = "FORTIFY_BUNDLE_SIGN_KEY_PATH"


def _truthy(value: str | None) -> bool:
    """Parse a boolean-ish env var ('1', 'true', 'yes' → True)."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Signature policy — single source of truth for the REQUIRE_SIGNATURE matrix
# ---------------------------------------------------------------------------


class SignaturePolicy:
    """The (pubkey, require_signature) pair resolved once from env.

    Concentrates every cell of the ``FORTIFY_BUNDLE_REQUIRE_SIGNATURE``
    × ``FORTIFY_BUNDLE_PUBKEY_PATH`` × (yaml | bundle-dir) matrix into
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

    Replaces three scattered helpers (one resolve-at-construction, one
    verify-at-fetch, one warn-at-announce) that the reviewer flagged as
    hard to audit cell-by-cell.
    """

    def __init__(self, *, verify_with: bytes | None, require_signature: bool) -> None:
        self.verify_with = verify_with
        self.require_signature = require_signature

    @classmethod
    def from_env(cls, override_path: str) -> "SignaturePolicy":
        """Build the policy from env vars, raising on require-without-key.

        ``override_path`` is the value of ``FORTIFY_LOCAL_POLICY`` — used
        only for error messages so the operator sees which load is
        being refused.
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
        either sign locally via ``FORTIFY_BUNDLE_SIGN_KEY_PATH`` or
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
                f"[fortify] warning: override bundle is signed but "
                f"{_BUNDLE_PUBKEY_ENV_VAR} is unset — signature NOT verified.",
                file=sys.stderr,
            )


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


def _local_policy_source(
    sig_policy: SignaturePolicy,
) -> PolicySource | None:
    """Resolve ``$FORTIFY_LOCAL_POLICY`` into a :class:`PolicySource`, if set.

    Dispatch by path shape:

      * ``<dir>`` → :class:`BundleDirPolicySource` (pre-built bundle from
        ``fortify policy build``; mtime-refreshed). Its ``verify_with``
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
        "directory (output of `fortify policy build`) or a .yaml file."
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
        f"[fortify] {_LOCAL_POLICY_ENV_VAR} active ({kind}): "
        f"{override_path} (wasm_hash={short}, {signed})",
        file=sys.stderr,
    )


def _apply_local_override(
    agent: AgentGraph,
    approval_handler: ApprovalHandler | None,
) -> AgentGraph | None:
    """Apply ``FORTIFY_LOCAL_POLICY`` to a freshly-built agent, if set.

    Returns the policy-wrapped agent with its source attached, or
    ``None`` when no override is configured — the caller then falls
    back to the normal :func:`enforce_policy` path with the agent's
    own packaged policy.

    Extracted from :func:`load_builtin_agent` + :func:`load_local_agent`
    which shared this exact block verbatim. :func:`load_fortify_agent`
    deliberately doesn't use this helper — its override interaction is
    more involved (it layers in alongside the platform-served bundle).
    """
    override = _local_policy_override()
    if override is None:
        return None
    bundle, source = override
    enforced = enforce_policy(agent, bundle, approval_handler=approval_handler)
    enforced._policy_source = source
    return enforced


def _local_policy_override() -> tuple[PolicyBundle, PolicySource] | None:
    """Resolve ``$FORTIFY_LOCAL_POLICY`` into a (bundle, source) pair.

    Returns ``None`` when the env var is unset. The bundle is the
    initial enforcement policy (ready to hand off to ``enforce_policy``);
    the source is attached to the agent so per-run refresh picks up
    yaml edits / bundle rebuilds without a restart.

    Failures (missing file, bad signature, opa not on PATH for a yaml
    source) raise loudly — silently degrading a security override
    would defeat the point. Signature-policy enforcement (the
    ``REQUIRE_SIGNATURE`` matrix) is centralised on
    :class:`SignaturePolicy`.
    """
    override_path = os.environ.get(_LOCAL_POLICY_ENV_VAR)
    if not override_path:
        return None
    # Build the signature policy once at startup. ``from_env`` raises
    # immediately if require-signature is set without a pubkey — fail
    # fast, before any agent code runs.
    sig_policy = SignaturePolicy.from_env(override_path)
    source = _local_policy_source(sig_policy)
    if source is None:
        # Defensive: we already null-checked override_path above; if
        # _local_policy_source returns None here it'd be an internal
        # invariant violation.
        return None
    bundle = source.fetch()
    if bundle is None:
        raise RuntimeError(
            f"{_LOCAL_POLICY_ENV_VAR}: source produced no bundle (internal "
            "invariant violated)."
        )
    # YamlPolicySource builds unsigned bundles unless FORTIFY_BUNDLE_SIGN_KEY_PATH
    # is set; strict mode refuses those. BundleDir's own constructor already
    # gated on verify_with, so this is a no-op for the dir path.
    if isinstance(source, YamlPolicySource):
        sig_policy.check_yaml_bundle(bundle, override_path)
    _announce_local_override(bundle, source, override_path)
    sig_policy.warn_if_unverified(bundle)
    return bundle, source


def _platform_bundle(payload: dict, client: FortifyClient) -> PolicyBundle | None:
    """Build + verify the signed bundle the platform served, if any.

    Thin one-shot wrapper around the shared decode-and-verify helper in
    :mod:`fortify.security.source`. Kept here for back-compat with code
    that wants a stateless decode of a single response — the stateful
    refresh path (``If-None-Match``, cache by ``wasm_hash``) lives in
    :class:`~fortify.security.source.PlatformPolicySource`.
    """
    from fortify.security.source import decode_and_verify_platform_bundle

    return decode_and_verify_platform_bundle(payload, client.public_key_bytes())


def builtin_agents_root() -> Path:
    """Return the filesystem path for packaged builtin agents."""
    return Path(str(files("fortify.agents.builtin")))


def _load_agent_spec_from_dir(agent_dir: Path) -> AgentSpec:
    """Load an agent spec from a directory containing agent.yaml."""
    spec_path = agent_dir / "agent.yaml"
    payload = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    return AgentSpec.model_validate(payload)


def local_agents_root(base_dir: str | Path | None = None) -> Path:
    """Return the local project root used for agent discovery."""
    return Path(base_dir or Path.cwd())


def iter_local_agent_dirs(base_dir: str | Path | None = None) -> list[Path]:
    """Discover local agent directories in the project root, ./agents, and ./examples."""
    root = local_agents_root(base_dir)
    discovered: dict[Path, None] = {}

    for child in root.iterdir():
        if child.is_dir() and (child / "agent.yaml").exists():
            discovered[child] = None

    for sub in ("agents", "examples"):
        sub_dir = root / sub
        if sub_dir.exists():
            for child in sub_dir.iterdir():
                if child.is_dir() and (child / "agent.yaml").exists():
                    discovered[child] = None

    return sorted(discovered)


def list_local_agents(base_dir: str | Path | None = None) -> list[str]:
    """List locally discoverable project agents."""
    names: list[str] = []
    for agent_dir in iter_local_agent_dirs(base_dir):
        spec = _load_agent_spec_from_dir(agent_dir)
        names.append(spec.name)
    return sorted(names)


def register_agent(name: str, factory: AgentFactory) -> None:
    """Register a code-defined agent factory under a stable id."""
    REGISTERED_AGENTS[name] = factory


def unregister_agent(name: str) -> None:
    """Remove a previously registered code-defined agent."""
    REGISTERED_AGENTS.pop(name, None)


def clear_registered_agents() -> None:
    """Clear the in-memory code agent registry."""
    REGISTERED_AGENTS.clear()


def list_registered_agents() -> list[str]:
    """List currently registered code-defined agent ids."""
    return sorted(REGISTERED_AGENTS)


def list_builtin_agents() -> list[str]:
    """List available packaged builtin agent names."""
    root = builtin_agents_root()
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and (path / "agent.yaml").exists()
    )


def load_builtin_agent_spec(name: str) -> AgentSpec:
    """Load a builtin agent specification by name."""
    agent_dir = builtin_agents_root() / name
    return _load_agent_spec_from_dir(agent_dir)


def load_builtin_agent_policy(name: str) -> AgentPolicy:
    """Load the policy associated with a builtin agent."""
    spec = load_builtin_agent_spec(name)
    return load_policy((builtin_agents_root() / name / spec.policy))


def find_local_agent_dir(name: str, base_dir: str | Path | None = None) -> Path:
    """Resolve a local agent name to its directory."""
    for agent_dir in iter_local_agent_dirs(base_dir):
        spec = _load_agent_spec_from_dir(agent_dir)
        if spec.name == name:
            return agent_dir
    raise KeyError(f'Unknown local agent "{name}"')


def load_local_agent_spec(name: str, base_dir: str | Path | None = None) -> AgentSpec:
    """Load a local agent specification by name."""
    return _load_agent_spec_from_dir(find_local_agent_dir(name, base_dir))


def load_local_agent_policy(
    name: str,
    base_dir: str | Path | None = None,
) -> AgentPolicy:
    """Load the policy associated with a local agent."""
    spec = load_local_agent_spec(name, base_dir)
    return load_policy(find_local_agent_dir(name, base_dir) / spec.policy)


def list_available_agents(base_dir: str | Path | None = None) -> list[str]:
    """List merged local and builtin agent ids."""
    names = set(list_builtin_agents())
    names.update(list_local_agents(base_dir))
    names.update(list_registered_agents())
    return sorted(names)


def resolve_builtin_tools(
    tool_names: list[str],
    extra_tools: Mapping[str, Any] | None = None,
) -> list[Any]:
    """Resolve tool ids against builtin and user-provided tool registries."""
    registry = dict(BUILTIN_TOOLS)
    registry.update(extra_tools or {})

    resolved: list[Any] = []
    for tool_name in tool_names:
        try:
            resolved.append(registry[tool_name])
        except KeyError as exc:
            raise KeyError(f'Unknown tool "{tool_name}"') from exc
    return resolved


def load_builtin_agent(
    name: str,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
    approval_handler: ApprovalHandler | None = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Load and instantiate a packaged builtin agent."""
    spec = load_builtin_agent_spec(name)
    agent_dir = builtin_agents_root() / name
    system_prompt = (agent_dir / spec.system_prompt).read_text(encoding="utf-8")
    policy: object = load_policy(agent_dir / spec.policy)
    tools = resolve_builtin_tools(spec.tools, extra_tools=extra_tools)
    agent, handler = create_agent(
        model=model or spec.model,
        tools=tools,
        system_prompt=system_prompt,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        name=spec.name,
    )
    overridden = _apply_local_override(agent, approval_handler)
    if overridden is not None:
        return overridden, handler
    return enforce_policy(agent, policy, approval_handler=approval_handler), handler


def load_local_agent(
    name: str,
    *,
    base_dir: str | Path | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
    approval_handler: ApprovalHandler | None = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Load and instantiate a local project agent."""
    spec = load_local_agent_spec(name, base_dir)
    agent_dir = find_local_agent_dir(name, base_dir)
    system_prompt = (agent_dir / spec.system_prompt).read_text(encoding="utf-8")
    policy: object = load_policy(agent_dir / spec.policy)
    tools = resolve_builtin_tools(spec.tools, extra_tools=extra_tools)
    agent, handler = create_agent(
        model=model or spec.model,
        tools=tools,
        system_prompt=system_prompt,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        name=spec.name,
    )
    overridden = _apply_local_override(agent, approval_handler)
    if overridden is not None:
        return overridden, handler
    return enforce_policy(agent, policy, approval_handler=approval_handler), handler


def load_registered_agent(
    name: str,
    *,
    base_dir: str | Path | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
    approval_handler: ApprovalHandler | None = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Load a registered code-defined agent by id."""
    try:
        factory = REGISTERED_AGENTS[name]
    except KeyError as exc:
        raise KeyError(f'Unknown registered agent "{name}"') from exc
    agent, handler = factory(
        base_dir=base_dir,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        extra_tools=extra_tools,
        model=model,
    )
    # Registered factories don't know about the CLI's approval flow; layer it
    # on after the fact by re-stamping each policy-wrapped tool.
    if approval_handler is not None:
        agent = _apply_approval_handler(agent, approval_handler)
    return agent, handler


def _apply_approval_handler(
    agent: AgentGraph, approval_handler: ApprovalHandler | None
) -> AgentGraph:
    """Re-stamp every :class:`GuardedTool` on ``agent`` with ``approval_handler``.

    For code-registered agents whose factories ran ``enforce_policy``
    internally and never saw the CLI's approval callback. Pass the
    ``GuardedTool`` itself (not its inner tool) so the idempotent
    re-wrap branch preserves the existing enforcer. Logs a warning
    when the agent has no ``GuardedTool`` tools (e.g. registered agent
    backed by a non-LangChain framework) so the caller knows the
    handler was silently dropped.
    """
    import logging

    from fortify.adapters.langchain.tools import GuardedTool

    rewrapped: list[Any] = []
    touched = False
    for tool_spec in agent.tools:
        if isinstance(tool_spec, GuardedTool):
            rewrapped.append(
                GuardedTool.wrap(
                    tool_spec,
                    approval_handler=approval_handler,
                )
            )
            touched = True
        else:
            rewrapped.append(tool_spec)
    if not touched:
        logging.getLogger(__name__).warning(
            "approval_handler was supplied but %r has no GuardedTool tools to apply "
            "it to; approval prompts will not fire for this agent",
            getattr(agent, "name", None) or "agent",
        )
        return agent
    return agent.with_tools(rewrapped)


def resolve_agent_source(name: str, base_dir: str | Path | None = None) -> AgentSource:
    """Return whether an agent id resolves from local or builtin definitions."""
    if name in list_local_agents(base_dir):
        return "local"
    if name in list_registered_agents():
        return "registered"
    if name in list_builtin_agents():
        return "builtin"
    raise KeyError(f'Unknown agent "{name}"')


def load_fortify_agent(
    name: str | None = None,
    *,
    project_id: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
    approval_handler: ApprovalHandler | None = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Fetch an agent from Fortify and return it with policy enforcement applied.

    Mirrors `load_local_agent` but sources the three YAMLs (agent, policy, system)
    from the Fortify API instead of disk. Tool resolution and enforcement are
    identical — only the bytes' origin differs.

    ``name`` is required. The Phase-7 env-var fallback chain
    (explicit → FORTIFY_AGENT_NAME → "default") was removed when the
    canonical serve path moved to ``build_runtime_from_local_agent``
    (which derives the name from the agent object). Direct callers of
    this API must pass an explicit name.

    The returned agent carries a ``fortify_client`` attribute referencing the
    :class:`~fortify.cloud.FortifyClient` used to fetch it; the runtime reads
    this attribute when an :class:`~fortify.runtime.User` scope is active to
    mint per-request attenuated tokens lazily.
    """
    if not name:
        raise ValueError(
            "load_fortify_agent(name=...) requires an explicit agent name. "
            "FORTIFY_AGENT_NAME / 'default' fallback was removed in Phase 7."
        )
    resolved_name = name
    config = FortifyConfig.from_env(
        project_id=project_id, base_url=base_url, api_key=api_key
    )
    client = FortifyClient(config)
    payload, initial_etag = client.get_agent(resolved_name)
    assert payload is not None, "first get_agent has no If-None-Match — 304 impossible"

    spec = AgentSpec.model_validate(yaml.safe_load(payload["agent_yaml"]) or {})
    system_prompt = payload.get("system_md") or ""

    tools = resolve_builtin_tools(spec.tools, extra_tools=extra_tools)

    agent, handler = create_agent(
        model=model or spec.model,
        tools=tools,
        system_prompt=system_prompt,
        session_id=session_id,
        # ``config.project_id`` can be None when the bearer envelope
        # didn't carry the project prefix (Phase 6 made it optional);
        # filter Nones so langchain doesn't get fed a None tag.
        tags=tags or [
            t for t in ["fortify", "fortify-cloud", config.project_id] if t
        ],
        name=spec.name,
    )
    # Policy precedence:
    #   1. FORTIFY_LOCAL_POLICY override (dev iteration) — wins outright,
    #      with its own mtime-driven refresh source.
    #   2. Platform-served signed bundle — verified, WASM-enforced — or the
    #      pydantic engine on the served policy_yaml when no bundle compiled
    #      (REQUIRE_SIGNATURE forbids that fallback).
    # Both arms come back as (engine, refresh source); the platform-side
    # decode/verify/fallback rules live in fortify.security.binding so this
    # loader and PolicyBinding.resolve can never drift.
    from fortify.security.binding import platform_policy_from_payload

    override = _local_policy_override()
    if override is not None:
        # Local override wins; the platform's bundle (if any) is ignored.
        policy, refresh_source = override
    else:
        policy, refresh_source = platform_policy_from_payload(
            client, resolved_name, payload, initial_etag
        )

    enforced = enforce_policy(agent, policy, approval_handler=approval_handler)
    # Attach the client so the runtime can do lazy attenuation inside an
    # active User scope without the caller having to thread it through.
    enforced.fortify_client = client
    enforced._policy_source = refresh_source
    return enforced, handler


def load_agent(
    name: str | None = None,
    *,
    base_dir: str | Path | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: Mapping[str, Any] | None = None,
    model: str | None = None,
    local_only: bool = False,
    approval_handler: ApprovalHandler | None = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Load an agent from Fortify (when FORTIFY_KEY is set), local, or builtin.

    ``name`` is required for every path post-Phase 7 — the
    FORTIFY_AGENT_NAME env-var fallback was removed when ``fortify
    serve`` moved to the uvicorn-style ``module:attr`` spec.

    Pass ``local_only=True`` to force resolution from local / registered /
    builtin sources even when ``FORTIFY_KEY`` is set in the environment.
    Useful for terminal-chat workflows that don't need cloud-fetched policy.
    """
    if not local_only and os.environ.get("FORTIFY_KEY"):
        # load_fortify_agent dropped its reserved ``user_id`` placeholder
        # in phase 3.5 — per-request user identity comes from a User scope
        # at invocation time, not from the loader.
        return load_fortify_agent(
            name,
            session_id=session_id,
            tags=tags,
            extra_tools=extra_tools,
            model=model,
            approval_handler=approval_handler,
        )
    if name is None:
        raise ValueError("load_agent() requires a name when not using Fortify Cloud")
    source = resolve_agent_source(name, base_dir)
    if source == "local":
        return load_local_agent(
            name,
            base_dir=base_dir,
            session_id=session_id,
            user_id=user_id,
            tags=tags,
            extra_tools=extra_tools,
            model=model,
            approval_handler=approval_handler,
        )
    if source == "registered":
        return load_registered_agent(
            name,
            base_dir=base_dir,
            session_id=session_id,
            user_id=user_id,
            tags=tags,
            extra_tools=extra_tools,
            model=model,
            approval_handler=approval_handler,
        )
    return load_builtin_agent(
        name,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        extra_tools=extra_tools,
        model=model,
        approval_handler=approval_handler,
    )
