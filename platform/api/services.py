import hashlib
import json
import secrets
import string

from sqlmodel import Session, select

from models import Agent, AgentVersion, DevToken, Project, Tool
from schemas import AgentManifest, ToolDefinition
from seeds import DEFAULT_AGENT_NAME, SEED_AGENTS

DEFAULT_PROJECT_ID = "support-bot"
DEFAULT_PROJECT_NAME = "support-bot"
PROTECTED_AGENT_NAMES = {DEFAULT_AGENT_NAME}

_SECRET_ALPHABET = string.ascii_letters + string.digits
_SECRET_LEN = 32

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


def update_agent(
    session: Session,
    project_id: str,
    name: str,
    *,
    agent_yaml: str | None = None,
    policy_yaml: str | None = None,
    system_md: str | None = None,
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
    agent.updated_at = datetime.now(timezone.utc)
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return agent


def mint_dev_token(
    session: Session,
    project_id: str,
    name: str,
    scopes: list[str],
    env: str,
) -> tuple[DevToken, str]:
    """Create a new dev token. Returns the row + the full secret (shown once)."""
    secret_chars = "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(_SECRET_LEN))
    prefix = f"fty_{env}"
    full_token = f"{prefix}_{project_id}_{secret_chars}"

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
    """Return e.g. `fty_live_8F3d…k29P` for list display."""
    if len(full) <= 16:
        return full
    head = full[:12]
    tail = full[-4:]
    return f"{head}\u2026{tail}"


# --- Agent manifest registration --------------------------------------------


def compute_manifest_hash(manifest: AgentManifest) -> str:
    """Reproducible SHA-256 of an agent manifest.

    Canonical JSON encoding (sorted keys, no whitespace) so the same manifest
    always hashes to the same hex digest regardless of Python dict ordering.
    """
    payload = manifest.model_dump(mode="json")
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
