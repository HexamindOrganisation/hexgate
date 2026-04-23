import secrets
import string

from sqlmodel import Session, select

from models import DevToken, Project

DEFAULT_PROJECT_ID = "support-bot"
DEFAULT_PROJECT_NAME = "support-bot"

_SECRET_ALPHABET = string.ascii_letters + string.digits
_SECRET_LEN = 32


def ensure_default_project(session: Session) -> Project:
    project = session.get(Project, DEFAULT_PROJECT_ID)
    if project is None:
        project = Project(id=DEFAULT_PROJECT_ID, name=DEFAULT_PROJECT_NAME)
        session.add(project)
        session.commit()
        session.refresh(project)
    return project


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
        id=f"tok_{secrets.token_hex(6)}",
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
