from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=utcnow)


class DevToken(SQLModel, table=True):
    id: str = Field(primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    name: str
    prefix: str  # "fty_test" or "fty_live"
    secret: str  # full token value; opaque random string for Phase A
    scopes_csv: str = ""  # comma-separated for now
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: Optional[datetime] = None
