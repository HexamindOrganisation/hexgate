from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel, UniqueConstraint


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


class Agent(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_agent_project_name"),
    )

    id: str = Field(primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    name: str = Field(index=True)
    agent_yaml: str
    policy_yaml: str  # the "default" role policy; back-compat for single-policy agents
    system_md: str = ""
    # Role-aware policy bundle: {role_name: policy_yaml_text}. Empty dict means
    # "single-policy agent" — the SDK falls back to `policy_yaml`. Populated
    # means the SDK builds a PolicySet via load_policy_map.
    roles_json: dict = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}")
    )
    updated_at: datetime = Field(default_factory=utcnow)


class AgentVersion(SQLModel, table=True):
    __tablename__ = "agent_version"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_version"),
        UniqueConstraint("agent_id", "content_hash", name="uq_agent_content_hash"),
    )

    id: str = Field(primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    version: int
    description: Optional[str] = None
    content_hash: str
    manifest: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class Tool(SQLModel, table=True):
    __tablename__ = "tool"
    __table_args__ = (
        UniqueConstraint("agent_version_id", "name", name="uq_tool_agent_version_name"),
    )

    id: str = Field(primary_key=True)
    agent_version_id: str = Field(foreign_key="agent_version.id", index=True)
    name: str
    description: Optional[str] = None
    input_schema: dict = Field(sa_column=Column(JSON, nullable=False))
