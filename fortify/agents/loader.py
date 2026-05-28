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
from fortify.cloud.client import FortifyClient, FortifyConfig, resolve_agent_name
from fortify.security import AgentPolicy, PolicyBundle, load_policy
from fortify.security.bundle import (
    BundleIntegrityError,
    BundleLoadError,
    BundleSignatureError,
)
from fortify.security.signing import SignatureError, decode_key
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


def _truthy(value: str | None) -> bool:
    """Parse a boolean-ish env var ('1', 'true', 'yes' → True)."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _verify_bundle_signature(bundle: PolicyBundle, override_path: str) -> None:
    """Apply the signature policy to a locally-loaded override bundle.

    The matrix, controlled by two env vars:

      * ``FORTIFY_BUNDLE_PUBKEY_PATH`` — path to a base64url public key.
      * ``FORTIFY_BUNDLE_REQUIRE_SIGNATURE`` — when truthy, refuse
        anything that isn't signed-and-verified.

    | signature | pubkey set | require | outcome              |
    |-----------|-----------|---------|----------------------|
    | present   | yes       | either  | verify; raise if bad |
    | present   | no        | false   | warn (can't verify)  |
    | present   | no        | true    | refuse (no key)      |
    | absent    | —         | false   | warn + proceed       |
    | absent    | —         | true    | refuse               |

    Local dev (unsigned bundles via `fortify policy build`) stays
    frictionless by default; CI/prod opt into strictness with
    REQUIRE_SIGNATURE=true.
    """
    import sys

    require = _truthy(os.environ.get(_REQUIRE_SIGNATURE_ENV_VAR))
    pubkey_path = os.environ.get(_BUNDLE_PUBKEY_ENV_VAR)

    if not bundle.is_signed:
        if require:
            raise RuntimeError(
                f"{_REQUIRE_SIGNATURE_ENV_VAR} is set but the bundle at "
                f"{override_path!r} has no signature (no *.bundle.json.sig). "
                "Build it with `fortify policy build --sign-key ...`."
            )
        print(
            f"[fortify] warning: override bundle at {override_path} is "
            "unsigned — authenticity not verified. Set "
            f"{_REQUIRE_SIGNATURE_ENV_VAR}=true to refuse unsigned bundles.",
            file=sys.stderr,
        )
        return

    # Bundle is signed. We need a public key to check it against.
    if not pubkey_path:
        if require:
            raise RuntimeError(
                f"{_REQUIRE_SIGNATURE_ENV_VAR} is set and the bundle is signed, "
                f"but {_BUNDLE_PUBKEY_ENV_VAR} is unset — no key to verify "
                "against."
            )
        print(
            f"[fortify] warning: override bundle is signed but "
            f"{_BUNDLE_PUBKEY_ENV_VAR} is unset — signature NOT verified.",
            file=sys.stderr,
        )
        return

    try:
        public_key_raw = decode_key(Path(pubkey_path).read_text(encoding="utf-8").strip())
    except (OSError, SignatureError) as exc:
        raise RuntimeError(
            f"{_BUNDLE_PUBKEY_ENV_VAR}={pubkey_path!r} could not be read as a "
            f"base64url public key: {exc}"
        ) from exc

    try:
        bundle.verify_signature(public_key_raw)
    except BundleSignatureError as exc:
        raise RuntimeError(
            f"override bundle at {override_path!r} failed signature "
            f"verification: {exc}"
        ) from exc


def _local_policy_override() -> PolicyBundle | None:
    """Load a :class:`PolicyBundle` from ``$FORTIFY_LOCAL_POLICY`` if set.

    The env var points at a directory containing a ``*.bundle.json`` +
    matching ``.yaml`` / ``.rego`` / ``.wasm``. Hash-integrity is checked
    eagerly so a stale or corrupt bundle fails loudly at startup rather
    than at the first denied tool call. Signature (authenticity) is then
    checked per :func:`_verify_bundle_signature`.

    A loud stderr line announces the override — silent overrides of
    security-relevant config would be a footgun, especially when the dev
    forgets they have the env var set.
    """
    override_path = os.environ.get(_LOCAL_POLICY_ENV_VAR)
    if not override_path:
        return None
    try:
        bundle = PolicyBundle.from_disk(Path(override_path))
        bundle.verify_integrity()
    except (BundleLoadError, BundleIntegrityError) as exc:
        raise RuntimeError(
            f"{_LOCAL_POLICY_ENV_VAR} is set to {override_path!r} but the "
            f"bundle could not be loaded: {exc}"
        ) from exc

    _verify_bundle_signature(bundle, override_path)

    import sys

    short = bundle.wasm_hash[:12] if bundle.wasm_hash else "?"
    signed = "signed" if bundle.is_signed else "unsigned"
    print(
        f"[fortify] {_LOCAL_POLICY_ENV_VAR} active: "
        f"{override_path} (wasm_hash={short}, {signed})",
        file=sys.stderr,
    )
    return bundle


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
    override = _local_policy_override()
    if override is not None:
        policy = override
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
    override = _local_policy_override()
    if override is not None:
        policy = override
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

    Agent name resolution: explicit arg → FORTIFY_AGENT_NAME env → "default".
    Every project is guaranteed to have a `default` agent, so zero-config use
    (set only FORTIFY_KEY) works.

    Mirrors `load_local_agent` but sources the three YAMLs (agent, policy, system)
    from the Fortify API instead of disk. Tool resolution and enforcement are
    identical — only the bytes' origin differs.

    The returned agent carries a ``fortify_client`` attribute referencing the
    :class:`~fortify.cloud.FortifyClient` used to fetch it; the runtime reads
    this attribute when an :class:`~fortify.runtime.User` scope is active to
    mint per-request attenuated tokens lazily.
    """
    from fortify.security.policy_set import load_policy_set_from_dict

    resolved_name = resolve_agent_name(name)
    config = FortifyConfig.from_env(
        project_id=project_id, base_url=base_url, api_key=api_key
    )
    client = FortifyClient(config)
    payload, initial_etag = client.get_agent(resolved_name)
    assert payload is not None, "first get_agent has no If-None-Match — 304 impossible"

    spec = AgentSpec.model_validate(yaml.safe_load(payload["agent_yaml"]) or {})
    system_prompt = payload.get("system_md") or ""

    # The platform returns one canonical ``policy.yaml`` text per agent. The
    # role bundle lives inline under a top-level ``roles:`` key when present;
    # otherwise the document is a flat single-policy doc. load_policy_set_from_dict
    # dispatches on shape — inheritance + mixin filtering applied either way.
    policy_payload = yaml.safe_load(payload["policy_yaml"]) or {}
    policy: object = load_policy_set_from_dict(policy_payload)

    tools = resolve_builtin_tools(spec.tools, extra_tools=extra_tools)

    agent, handler = create_agent(
        model=model or spec.model,
        tools=tools,
        system_prompt=system_prompt,
        session_id=session_id,
        tags=tags or ["fortify", "fortify-cloud", config.project_id],
        name=spec.name,
    )
    # Policy precedence:
    #   1. FORTIFY_LOCAL_POLICY override (dev iteration) — wins outright.
    #   2. Platform-served signed bundle — verified, WASM-enforced.
    #   3. policy_yaml + pydantic — fallback when no bundle was served.
    from fortify.security.source import PlatformPolicySource

    override = _local_policy_override()
    platform_source: PlatformPolicySource | None = None
    if override is not None:
        policy = override
        # Local override has its own (8b) refresh mechanism — no platform
        # source attached.
    else:
        platform_bundle = _platform_bundle(payload, client)
        if platform_bundle is not None:
            policy = platform_bundle
        elif _truthy(os.environ.get(_REQUIRE_SIGNATURE_ENV_VAR)):
            raise RuntimeError(
                f"{_REQUIRE_SIGNATURE_ENV_VAR} is set but the platform served "
                f"no signed bundle for agent {resolved_name!r} — the policy may "
                "not have compiled (is opa available on the control plane?). "
                "Refusing to fall back to the pydantic engine."
            )
        # Phase 8a: attach a PolicySource so refresh_policy() can pull
        # updates at every agent run via If-None-Match. Pre-seed with the
        # bundle + etag we just fetched so the first refresh is a 304
        # unless the policy actually changed.
        platform_source = PlatformPolicySource(
            client,
            resolved_name,
            initial_bundle=platform_bundle,
            initial_etag=initial_etag,
        )

    enforced = enforce_policy(agent, policy, approval_handler=approval_handler)
    # Attach the client so the runtime can do lazy attenuation inside an
    # active User scope without the caller having to thread it through.
    enforced.fortify_client = client
    if platform_source is not None:
        enforced._policy_source = platform_source
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

    When FORTIFY_KEY is set, `name` is optional: the SDK falls back to
    FORTIFY_AGENT_NAME and finally to `"default"`. For the local/builtin
    paths, `name` is required — we can't guess which local directory you
    meant.

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
