import secrets

from sqlmodel import Session, select

from biscuits import MintRequest, make_envelope, mint_token
from models import Agent, DevToken, Project
from seeds import DEFAULT_AGENT_NAME, SEED_AGENTS

DEFAULT_PROJECT_ID = "support-bot"
DEFAULT_PROJECT_NAME = "support-bot"
PROTECTED_AGENT_NAMES = {DEFAULT_AGENT_NAME}


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
                id=f"agt_{secrets.token_hex(6)}",
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
    token_id = f"tok_{secrets.token_hex(6)}"
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
        id=token_id,
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
    stmt = select(DevToken).where(DevToken.project_id == project_id).order_by(DevToken.created_at.desc())  # type: ignore[attr-defined]
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
