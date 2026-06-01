import hashlib
import json
import logging
import os
import secrets
from typing import Callable

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from models import (
    Agent,
    AgentVersion,
    DevToken,
    Organization,
    OrganizationMember,
    Project,
    Tool,
    User,
)
from schemas import AgentManifest, ToolDefinition
from biscuits import MintRequest, make_envelope, mint_token
from seeds import DEFAULT_AGENT_NAME, SEED_AGENTS

logger = logging.getLogger("fortify.platform.services")

# Triple-default seed identity (M3). Fixed UUIDs so every fresh dev DB
# produces identical rows — tests and integration scripts can reference
# these constants directly instead of looking up by name.
#
# Production (hosted HexaGate) sets FORTIFY_SEED=skip to start with a
# truly empty DB. Self-hosters and `make platform-api` get a working
# install on first boot without any setup.
DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_ORG_SLUG = "default"
DEFAULT_ORG_NAME = "Default Organization"

DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000002"
DEFAULT_USER_EMAIL = "admin@hexagate.local"

DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000003"
DEFAULT_PROJECT_NAME = "support-bot"

DEFAULT_MEMBERSHIP_ID = "00000000-0000-0000-0000-000000000004"

PROTECTED_AGENT_NAMES = {DEFAULT_AGENT_NAME}


def _seed_disabled() -> bool:
    """``FORTIFY_SEED=skip`` opts a deployment out of the triple-default."""
    return os.environ.get("FORTIFY_SEED", "").strip().lower() == "skip"


def _announce_default_admin_credentials(email: str, password: str) -> None:
    """Loud one-shot stderr print of the freshly-generated admin password.

    Same posture as ``FileKeyStore._announce_first_run`` — operators
    only see this once, ever; subsequent boots are silent. The password
    is never logged again from anywhere in the codebase.
    """
    import sys

    bar = "=" * 72
    print(
        f"\n{bar}\n"
        f"FIRST-BOOT DEFAULT ADMIN CREDENTIALS\n"
        f"   email:    {email}\n"
        f"   password: {password}\n\n"
        f"This is printed ONCE on first boot. Save it now — there is no\n"
        f"second display. Sign in at the dashboard and rotate the password\n"
        f"via your account settings as soon as you're in.\n"
        f"\n"
        f"Self-hosted deployments that don't want a default account at\n"
        f"all should set FORTIFY_SEED=skip and POST /v1/auth/register\n"
        f"to bootstrap their first user from scratch.\n"
        f"{bar}\n",
        file=sys.stderr,
        flush=True,
    )


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


async def ensure_default_seed(session: AsyncSession) -> Project | None:
    """Idempotently create the triple-default: Org + User + Membership + Project + agents.

    First-boot UX for self-hosters and `make platform-api`. Every step is
    individually idempotent so calling this on an already-seeded DB is a
    no-op — same shape `ensure_default_project` used to have, just broader.

    Returns the default Project, or ``None`` when ``FORTIFY_SEED=skip``
    is set (production hosted deployments). When skipped, callers must
    handle the empty-DB case explicitly — there is no implicit project.
    """
    if _seed_disabled():
        return None

    # Org first — Project FKs to it, so it has to exist before the project.
    org = await session.get(Organization, DEFAULT_ORG_ID)
    if org is None:
        org = Organization(
            id=DEFAULT_ORG_ID,
            slug=DEFAULT_ORG_SLUG,
            name=DEFAULT_ORG_NAME,
        )
        session.add(org)

    # Default admin user. M3 Phase 3a: first boot generates a fresh
    # random password, hashes it via FastAPI Users' PasswordHelper, and
    # prints the plaintext to stderr ONCE for the operator to copy. On
    # every subsequent boot the row already exists → no print, no
    # re-hash, no behaviour change. Production deployments that don't
    # want a default account set FORTIFY_SEED=skip and create their
    # first user via POST /v1/auth/register instead.
    user = await session.get(User, DEFAULT_USER_ID)
    if user is None:
        from fastapi_users.password import PasswordHelper

        password_plain = secrets.token_urlsafe(16)
        hashed = PasswordHelper().hash(password_plain)
        user = User(
            id=DEFAULT_USER_ID,
            email=DEFAULT_USER_EMAIL,
            hashed_password=hashed,
            is_active=True,
            # Default seed user is auto-verified — no email flow runs at
            # `make platform-api`. Real registered users start unverified.
            is_verified=True,
            is_superuser=True,
        )
        session.add(user)
        _announce_default_admin_credentials(DEFAULT_USER_EMAIL, password_plain)

    # Owner membership wiring user → org. The unique constraint on
    # (user_id, org_id) makes this safe to re-add on subsequent boots.
    member = await session.get(OrganizationMember, DEFAULT_MEMBERSHIP_ID)
    if member is None:
        member = OrganizationMember(
            id=DEFAULT_MEMBERSHIP_ID,
            user_id=DEFAULT_USER_ID,
            org_id=DEFAULT_ORG_ID,
            role="owner",
        )
        session.add(member)

    project = await session.get(Project, DEFAULT_PROJECT_ID)
    if project is None:
        project = Project(
            id=DEFAULT_PROJECT_ID,
            org_id=DEFAULT_ORG_ID,
            name=DEFAULT_PROJECT_NAME,
        )
        session.add(project)

    await session.commit()
    await session.refresh(project)
    # Always ensure seeded agents exist — idempotent, so existing projects
    # pick up the `default` guarantee on any subsequent boot.
    await ensure_seeded_agents(session, project.id)
    return project


# Back-compat alias for callers that still use the old name. New code uses
# ``ensure_default_seed`` directly; this one-liner keeps existing imports
# (main.py, tests) working without a renaming sweep this turn.
ensure_default_project = ensure_default_seed


async def ensure_seeded_agents(session: AsyncSession, project_id: str) -> None:
    """Idempotently add any missing seeded agents to a project."""
    existing = {a.name for a in await list_agents(session, project_id)}
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
        await session.commit()


async def list_agents(session: AsyncSession, project_id: str) -> list[Agent]:
    stmt = select(Agent).where(Agent.project_id == project_id).order_by(Agent.name)  # type: ignore[attr-defined]
    return list((await session.exec(stmt)).all())


async def get_agent(
    session: AsyncSession, project_id: str, name: str
) -> Agent | None:
    stmt = select(Agent).where(Agent.project_id == project_id, Agent.name == name)
    return (await session.exec(stmt)).first()


async def get_latest_agent_versions_map(
    session: AsyncSession, agent_ids: list[str]
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
    return {version.agent_id: version for version in (await session.exec(statement)).all()}


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

    Stays sync because it doesn't touch the DB — only shells out to ``opa``
    via the SDK. Callers run it inside an async handler via the default
    threadpool (``asyncio.to_thread``) if they need to keep the event loop
    responsive during a long compile; for our tiny policies a direct call
    is fine.
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


async def update_agent(
    session: AsyncSession,
    project_id: str,
    name: str,
    *,
    agent_yaml: str | None = None,
    policy_yaml: str | None = None,
    system_md: str | None = None,
    sign: Callable[[bytes], bytes] | None = None,
) -> Agent | None:
    from datetime import datetime, timezone

    agent = await get_agent(session, project_id, name)
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
    await session.commit()
    await session.refresh(agent)
    return agent


async def backfill_bundles(
    session: AsyncSession, sign: Callable[[bytes], bytes]
) -> int:
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
    agents = (await session.exec(select(Agent))).all()
    for agent in agents:
        if agent.compiled_wasm is not None:
            continue
        bundle = compile_bundle(agent.policy_yaml, sign)
        if bundle is None:
            continue
        agent.compiled_wasm, agent.bundle_manifest, agent.bundle_signature = bundle
        session.add(agent)
        count += 1
    if count:
        await session.commit()
    return count


async def mint_dev_token(
    session: AsyncSession,
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
    await session.commit()
    await session.refresh(token)
    return token, full_token


async def list_dev_tokens(
    session: AsyncSession, project_id: str
) -> list[DevToken]:
    stmt = (
        select(DevToken)
        .where(DevToken.project_id == project_id)
        .order_by(DevToken.created_at.desc())
    )  # type: ignore[attr-defined]
    return list((await session.exec(stmt)).all())


async def find_token_by_secret(
    session: AsyncSession, secret: str
) -> DevToken | None:
    """Look up a token by its full secret value. Updates last_used_at on hit."""
    from datetime import datetime, timezone

    stmt = select(DevToken).where(DevToken.secret == secret)
    token = (await session.exec(stmt)).first()
    if token is not None:
        token.last_used_at = datetime.now(timezone.utc)
        session.add(token)
        await session.commit()
    return token


async def delete_dev_token(
    session: AsyncSession, project_id: str, token_id: str
) -> bool:
    token = await session.get(DevToken, token_id)
    if token is None or token.project_id != project_id:
        return False
    await session.delete(token)
    await session.commit()
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
    return f"{head}…{tail}"


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


async def register_manifest(
    session: AsyncSession,
    project_id: str,
    manifest: AgentManifest,
) -> tuple[AgentVersion, bool]:
    """Upsert an agent + version from an AgentManifest.

    Returns ``(version, created)`` where ``created`` is False if a version
    with the same content_hash already existed under this agent — in which
    case nothing is written and the existing row is returned.
    """
    content_hash = compute_manifest_hash(manifest)
    agent, agent_created = await _get_or_create_agent(session, project_id, manifest.name)

    if not agent_created:
        existing = await _find_version_by_hash(session, agent.id, content_hash)
        if existing is not None:
            return existing, False

    next_version = 1 if agent_created else await _next_version_number(session, agent.id)
    version = await _create_agent_version(
        session, agent.id, manifest, content_hash, next_version
    )
    await _create_tools(session, version.id, manifest.tools)

    await session.commit()
    await session.refresh(version)
    return version, True


async def _get_or_create_agent(
    session: AsyncSession, project_id: str, name: str
) -> tuple[Agent, bool]:
    """Return the Agent for (project_id, name), creating it if missing.

    The agent_yaml / policy_yaml columns are legacy NOT-NULL fields from the
    YAML-edited dashboard flow; code-defined agents leave them empty since the
    actual content lives on each AgentVersion.
    """
    agent = await get_agent(session, project_id, name)
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
    await session.flush()
    return agent, True


async def _find_version_by_hash(
    session: AsyncSession, agent_id: str, content_hash: str
) -> AgentVersion | None:
    """Return the existing AgentVersion with this content_hash, if any."""
    stmt = select(AgentVersion).where(
        AgentVersion.agent_id == agent_id,
        AgentVersion.content_hash == content_hash,
    )
    return (await session.exec(stmt)).first()


async def _next_version_number(session: AsyncSession, agent_id: str) -> int:
    """Return the next sequential version number for an agent."""
    last = (await session.exec(
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version.desc())  # type: ignore[attr-defined]
    )).first()
    return (last.version + 1) if last is not None else 1


async def _create_agent_version(
    session: AsyncSession,
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
    await session.flush()
    return row


async def _create_tools(
    session: AsyncSession,
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
