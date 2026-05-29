import hashlib
import json
import logging
import secrets
from typing import Callable

from sqlalchemy import func
from sqlmodel import Session, select

from models import Agent, AgentVersion, DevToken, Project, Tool
from schemas import AgentManifest, ToolDefinition
from biscuits import MintRequest, make_envelope, mint_token
from seeds import DEFAULT_AGENT_NAME, SEED_AGENTS

logger = logging.getLogger("fortify.platform.services")

DEFAULT_PROJECT_ID = "support-bot"
DEFAULT_PROJECT_NAME = "support-bot"
PROTECTED_AGENT_NAMES = {DEFAULT_AGENT_NAME}

# Prefix map for human-readable row IDs (e.g. agt_a1b2c3…). Centralized here so
# entropy / format changes happen in one place; class-keyed so a typo is a
# NameError at import, not a runtime bug.
_ID_PREFIXES: dict[type, str] = {
    Agent: "agt",
    AgentVersion: "agv",
    Tool: "tol",
    DevToken: "tok",
}


def new_id(kind: type) -> str:
    """Generate a prefixed row id for a SQLModel class, e.g. ``agt_a1b2…``."""
    return f"{_ID_PREFIXES[kind]}_{secrets.token_hex(6)}"


def ensure_default_project(session: Session) -> Project:
    project = session.get(Project, DEFAULT_PROJECT_ID)
    if project is None:
        project = Project(id=DEFAULT_PROJECT_ID, name=DEFAULT_PROJECT_NAME)
        session.add(project)
        session.commit()
        session.refresh(project)
    # Always ensure seeded agents exist — idempotent, so existing projects
    # pick up the `default` guarantee on any subsequent boot.
    ensure_seeded_agents(session, project.id)
    return project


def ensure_seeded_agents(session: Session, project_id: str) -> None:
    """Idempotently add any missing seeded agents to a project."""
    existing = {a.name for a in list_agents(session, project_id)}
    added = False
    for seed in SEED_AGENTS:
        if seed["name"] in existing:
            continue
        session.add(
            Agent(
                id=new_id(Agent),
                project_id=project_id,
                name=seed["name"],
                agent_yaml=seed["agent_yaml"],
                policy_yaml=seed["policy_yaml"],
                system_md=seed["system_md"],
            )
        )
        added = True
    if added:
        session.commit()


def list_agents(session: Session, project_id: str) -> list[Agent]:
    stmt = select(Agent).where(Agent.project_id == project_id).order_by(Agent.name)  # type: ignore[attr-defined]
    return list(session.exec(stmt))


def get_agent(session: Session, project_id: str, name: str) -> Agent | None:
    stmt = select(Agent).where(Agent.project_id == project_id, Agent.name == name)
    return session.exec(stmt).first()


def get_latest_agent_version_id(
    session: Session, project_id: str, agent_name: str
) -> str:
    """Return the latest registered AgentVersion.id for an agent.

    Used by the audit ingest endpoint to stamp the canonical version_id on
    each decision row from the platform's relational store, rather than
    trusting an SDK-supplied value. Returns ``""`` when:

      * no Agent row exists for ``(project_id, agent_name)`` — e.g. the
        decision came from a locally-overridden bundle, or an SDK is
        ingesting before its first /v1/agents registration.
      * the Agent exists but has no AgentVersion yet.

    Caller can treat the empty string as "unresolved" without branching.
    """
    agent = get_agent(session, project_id, agent_name)
    if agent is None:
        return ""
    stmt = (
        select(AgentVersion.id)
        .where(AgentVersion.agent_id == agent.id)
        .order_by(AgentVersion.version.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    return session.exec(stmt).first() or ""


def get_latest_agent_versions_map(
    session: Session, agent_ids: list[str]
) -> dict[str, AgentVersion]:
    """Return a map of {agent_id: latest AgentVersion}
    for a list of agent ids in a single query.

    Agents with no registered version are omitted from the map.
    """
    if not agent_ids:
        return {}
    max_version_per_agent = (
        select(
            AgentVersion.agent_id,
            func.max(AgentVersion.version).label("max_version"),
        )
        .where(AgentVersion.agent_id.in_(agent_ids))
        .group_by(AgentVersion.agent_id)
        .subquery()
    )
    statement = select(AgentVersion).join(
        max_version_per_agent,
        (AgentVersion.agent_id == max_version_per_agent.c.agent_id)
        & (AgentVersion.version == max_version_per_agent.c.max_version),
    )
    return {version.agent_id: version for version in session.exec(statement)}


def compile_bundle(
    policy_yaml: str, sign: Callable[[bytes], bytes]
) -> tuple[bytes, str, bytes] | None:
    """Compile ``policy_yaml`` to a signed WASM bundle.

    Runs the SDK's YAML → Rego → WASM compiler, builds a manifest with the
    content hashes, and signs the manifest's exact bytes with ``sign`` (the
    platform's root key). Returns ``(wasm_bytes, manifest_text, signature)``,
    or ``None`` when compilation can't happen — ``opa`` not installed, or the
    policy is malformed. A ``None`` return is not an error: the caller stores
    no bundle and the SDK falls back to the pydantic engine.

    This shells out to ``opa`` (via the SDK), which blocks. It's safe here
    because FastAPI runs sync route handlers in a worker thread, so the call
    never touches the event loop.
    """
    # Imported lazily so the platform still boots if the SDK / opa aren't
    # present — only save-time compilation needs them. build_signed_bundle
    # is the SAME helper `fortify policy build` uses, so the manifest format
    # and its byte-exact serialization can't drift between the two.
    from fortify.security import build_signed_bundle
    from fortify.security.rego_wasm import OpaNotFoundError

    try:
        bundle = build_signed_bundle(policy_yaml, sign=sign)
    except OpaNotFoundError:
        logger.warning(
            "compile_bundle: opa not on PATH — storing no bundle "
            "(SDK will fall back to pydantic). Install opa to ship signed bundles."
        )
        return None
    except Exception as exc:
        # Any other compile failure (bad constraint, schema error, opa build
        # error) degrades gracefully — the save still succeeds without a bundle.
        logger.warning("compile_bundle: policy did not compile: %s", exc)
        return None

    return bundle.wasm_bytes, bundle.manifest_bytes.decode("utf-8"), bundle.signature


def update_agent(
    session: Session,
    project_id: str,
    name: str,
    *,
    agent_yaml: str | None = None,
    policy_yaml: str | None = None,
    system_md: str | None = None,
    sign: Callable[[bytes], bytes] | None = None,
) -> Agent | None:
    from datetime import datetime, timezone

    agent = get_agent(session, project_id, name)
    if agent is None:
        return None
    if agent_yaml is not None:
        agent.agent_yaml = agent_yaml
    if policy_yaml is not None:
        agent.policy_yaml = policy_yaml
    if system_md is not None:
        agent.system_md = system_md

    # Recompile + re-sign the bundle from the (possibly updated) policy. We
    # always rebuild rather than diff so a fixed policy re-acquires a bundle
    # and a newly-broken one drops its stale (now-wrong) bundle.
    if sign is not None:
        bundle = compile_bundle(agent.policy_yaml, sign)
        if bundle is not None:
            agent.compiled_wasm, agent.bundle_manifest, agent.bundle_signature = bundle
        else:
            agent.compiled_wasm = None
            agent.bundle_manifest = None
            agent.bundle_signature = None

    agent.updated_at = datetime.now(timezone.utc)
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return agent


def backfill_bundles(session: Session, sign: Callable[[bytes], bytes]) -> int:
    """Compile + sign a bundle for every agent that doesn't already have one.

    Seeded agents are inserted directly (``ensure_default_project`` builds
    ``Agent(...)`` without the save-time compile hook), so on a fresh DB
    they start bundle-less and would be served via the pydantic fallback.
    Running this at startup means even a brand-new platform serves signed
    WASM bundles for the seeds on the very first request.

    Idempotent: agents that already carry a bundle are skipped, and a
    policy that won't compile (or a platform without opa) is simply left
    bundle-less. Returns the number of agents backfilled.
    """
    count = 0
    for agent in session.exec(select(Agent)).all():
        if agent.compiled_wasm is not None:
            continue
        bundle = compile_bundle(agent.policy_yaml, sign)
        if bundle is None:
            continue
        agent.compiled_wasm, agent.bundle_manifest, agent.bundle_signature = bundle
        session.add(agent)
        count += 1
    if count:
        session.commit()
    return count


def mint_dev_token(
    session: Session,
    project_id: str,
    name: str,
    scopes: list[str],
    env: str,
    *,
    signing_key_bytes: bytes,
) -> tuple[DevToken, str]:
    """Create a new dev token, signed as a Biscuit by the platform's root key.

    The wire format stays human-readable: ``fty_<env>_<project>_<biscuit_b64>``.
    Project id is duplicated in the prefix (for grep / GitHub-secret-scanning)
    and inside the Biscuit's claims (the source of truth at verification time).

    ``signing_key_bytes`` are the raw 32-byte Ed25519 private key from the
    platform's keystore. Pulled out of the keystore at the call site so this
    function stays decoupled from where the key actually lives.

    Returns the persisted row + the full token string (the b64 form is what
    the operator copies out of the dashboard — shown once, never stored
    in the row outside of the ``secret`` column for revocation lookup).
    """
    token_id = new_id(DevToken)
    biscuit_b64 = mint_token(
        signing_key_bytes,
        MintRequest(
            project_id=project_id,
            token_id=token_id,
            name=name,
            scopes=scopes,
            env=env,
            ttl_seconds=None,  # dev tokens don't expire by default; revoke explicitly.
        ),
    )
    prefix = f"fty_{env}"
    full_token = make_envelope(env, project_id, biscuit_b64)

    token = DevToken(
        id=new_id(DevToken),
        project_id=project_id,
        name=name,
        prefix=prefix,
        secret=full_token,
        scopes_csv=",".join(scopes),
    )
    session.add(token)
    session.commit()
    session.refresh(token)
    return token, full_token


def list_dev_tokens(session: Session, project_id: str) -> list[DevToken]:
    stmt = (
        select(DevToken)
        .where(DevToken.project_id == project_id)
        .order_by(DevToken.created_at.desc())
    )  # type: ignore[attr-defined]
    return list(session.exec(stmt))


def find_token_by_secret(session: Session, secret: str) -> DevToken | None:
    """Look up a token by its full secret value. Updates last_used_at on hit."""
    from datetime import datetime, timezone

    stmt = select(DevToken).where(DevToken.secret == secret)
    token = session.exec(stmt).first()
    if token is not None:
        token.last_used_at = datetime.now(timezone.utc)
        session.add(token)
        session.commit()
    return token


def delete_dev_token(session: Session, project_id: str, token_id: str) -> bool:
    token = session.get(DevToken, token_id)
    if token is None or token.project_id != project_id:
        return False
    session.delete(token)
    session.commit()
    return True


def mask_secret(full: str) -> str:
    """Return e.g. ``fty_live_8F3d…k29P`` for list display.

    Skips trailing ``=`` base64 padding when computing the tail so masked
    Biscuit envelopes don't end on a meaningless ``=`` character.
    """
    if len(full) <= 16:
        return full
    head = full[:12]
    body = full.rstrip("=")
    tail = body[-4:] if len(body) >= 4 else body
    return f"{head}\u2026{tail}"


# --- Agent manifest registration --------------------------------------------


def compute_manifest_hash(manifest: AgentManifest) -> str:
    """Reproducible SHA-256 of an agent manifest.

    Canonical JSON encoding (sorted keys, no whitespace) so the same manifest
    always hashes to the same hex digest regardless of Python dict ordering.

    ``exclude_none=True`` keeps hash continuity across schema growth: when a
    new ``Optional`` field lands with a ``None`` default, an old manifest
    re-registered against the new schema still produces the same digest it
    did before — so ``_find_version_by_hash`` matches and we don't create a
    duplicate ``AgentVersion`` row for what is functionally the same content.
    """
    payload = manifest.model_dump(mode="json", exclude_none=True)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def register_manifest(
    session: Session,
    project_id: str,
    manifest: AgentManifest,
) -> tuple[AgentVersion, bool]:
    """Upsert an agent + version from an AgentManifest.

    Returns ``(version, created)`` where ``created`` is False if a version
    with the same content_hash already existed under this agent \u2014 in which
    case nothing is written and the existing row is returned.
    """
    content_hash = compute_manifest_hash(manifest)
    agent, agent_created = _get_or_create_agent(session, project_id, manifest.name)

    if not agent_created:
        existing = _find_version_by_hash(session, agent.id, content_hash)
        if existing is not None:
            return existing, False

    next_version = 1 if agent_created else _next_version_number(session, agent.id)
    version = _create_agent_version(
        session, agent.id, manifest, content_hash, next_version
    )
    _create_tools(session, version.id, manifest.tools)

    session.commit()
    session.refresh(version)
    return version, True


def _get_or_create_agent(
    session: Session, project_id: str, name: str
) -> tuple[Agent, bool]:
    """Return the Agent for (project_id, name), creating it if missing.

    The agent_yaml / policy_yaml columns are legacy NOT-NULL fields from the
    YAML-edited dashboard flow; code-defined agents leave them empty since the
    actual content lives on each AgentVersion.
    """
    agent = get_agent(session, project_id, name)
    if agent is not None:
        return agent, False
    agent = Agent(
        id=new_id(Agent),
        project_id=project_id,
        name=name,
        agent_yaml="",
        policy_yaml="",
        system_md="",
    )
    session.add(agent)
    session.flush()
    return agent, True


def _find_version_by_hash(
    session: Session, agent_id: str, content_hash: str
) -> AgentVersion | None:
    """Return the existing AgentVersion with this content_hash, if any."""
    stmt = select(AgentVersion).where(
        AgentVersion.agent_id == agent_id,
        AgentVersion.content_hash == content_hash,
    )
    return session.exec(stmt).first()


def _next_version_number(session: Session, agent_id: str) -> int:
    """Return the next sequential version number for an agent."""
    last = session.exec(
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version.desc())  # type: ignore[attr-defined]
    ).first()
    return (last.version + 1) if last is not None else 1


def _create_agent_version(
    session: Session,
    agent_id: str,
    manifest: AgentManifest,
    content_hash: str,
    version: int,
) -> AgentVersion:
    """Create and persist a new AgentVersion row for `manifest`."""
    row = AgentVersion(
        id=new_id(AgentVersion),
        agent_id=agent_id,
        version=version,
        description=manifest.description,
        content_hash=content_hash,
        manifest=manifest.model_dump(mode="json"),
    )
    session.add(row)
    session.flush()
    return row


def _create_tools(
    session: Session,
    agent_version_id: str,
    tools: list[ToolDefinition],
) -> None:
    """Insert one Tool row per ToolDefinition under an agent version."""
    for tool in tools:
        session.add(
            Tool(
                id=new_id(Tool),
                agent_version_id=agent_version_id,
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema.model_dump(mode="json"),
            )
        )
