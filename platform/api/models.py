from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column, LargeBinary
from sqlmodel import Field, SQLModel, UniqueConstraint


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Identity + tenancy (M3 — multi-tenant platform)
#
# Three tables make HexaGate multi-tenant: ``User`` (a person), ``Organization``
# (a tenant — what customers see as their "workspace" or "team"), and
# ``OrganizationMember`` (the many-to-many that grants a user access to an
# org, with a role on the edge).
#
# Auth-specific columns on User (hashed_password, is_verified, OAuth accounts)
# land later when we wire FastAPI Users. For v1 schema, identity = an email
# we can correlate later; tenancy is the load-bearing structure that gates
# access to every other table.
# ---------------------------------------------------------------------------


class Organization(SQLModel, table=True):
    """A tenant. Customers see this as their workspace / team.

    All other tenant-scoped data (Projects, Agents via projects, etc.) hangs
    off ``Organization`` by FK. Tenant isolation is enforced at the API layer
    by checking ``OrganizationMember`` for the active user before every
    access to anything inside this org.
    """

    id: str = Field(primary_key=True)              # uuid
    # URL-safe stable identifier — used in human-facing URLs like
    # /orgs/{slug}/dashboard. Globally unique across the platform; mutable
    # (rarely — renaming the slug breaks bookmarks but that's the user's
    # call), but the immutable ``id`` is what every FK points at.
    slug: str = Field(index=True, unique=True)
    name: str
    created_at: datetime = Field(default_factory=utcnow)


class User(SQLModel, table=True):
    """A person. One email, one account, many org memberships.

    Auth-specific columns (hashed_password, is_verified, is_active,
    is_superuser, oauth_accounts) come in Phase 3 when we wire FastAPI
    Users — its ``SQLAlchemyBaseUserTableUUID`` extends this shape.
    """

    id: str = Field(primary_key=True)              # uuid
    email: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=utcnow)


class OrganizationMember(SQLModel, table=True):
    """User <-> Organization edge, with a role.

    A user can belong to many orgs; an org can have many members. The
    unique constraint on (user_id, org_id) enforces "at most one
    membership per pair" — role changes update the existing row.

    Role is a string (not an Enum) so we can add ``billing_admin`` /
    ``read_only`` / etc. without an Alembic migration; validation happens
    at the API layer.
    """

    __tablename__ = "organization_member"
    __table_args__ = (
        UniqueConstraint("user_id", "org_id", name="uq_org_member"),
    )

    id: str = Field(primary_key=True)              # surrogate uuid PK
    user_id: str = Field(foreign_key="user.id", index=True)
    org_id: str = Field(foreign_key="organization.id", index=True)
    role: str                                      # "owner" | "admin" | "member"
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Existing tables — Project gains an org_id FK so it inherits tenancy.
# ---------------------------------------------------------------------------


class Project(SQLModel, table=True):
    # UUID, immutable. Existing seed (``support-bot``) is reseeded with the
    # fixed ``DEFAULT_PROJECT_ID`` UUID in seeds.py so dev environments stay
    # reproducible across rebuilds.
    id: str = Field(primary_key=True)
    # Project belongs to exactly one org. Tenant isolation is enforced by
    # checking the active user's OrganizationMember row for this org_id
    # before any access to project-scoped data.
    org_id: str = Field(foreign_key="organization.id", index=True)
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
    agent_yaml: str  # manifest: name, model, tool list
    # Canonical policy document. May be a flat single-policy YAML (legacy
    # one-role-per-agent shape) or an inline-roles YAML with a top-level
    # ``roles:`` section. The SDK's load_policy_set_from_dict dispatches on
    # which shape is present.
    policy_yaml: str
    system_md: str = ""
    updated_at: datetime = Field(default_factory=utcnow)

    # Compiled + signed WASM bundle, produced from policy_yaml at save time
    # (see services.compile_bundle). Null when opa is unavailable or the
    # policy fails to compile — the SDK then falls back to the pydantic
    # engine on policy_yaml. The signature is over bundle_manifest's exact
    # bytes, signed by the platform's root key (the same key that signs
    # biscuits), so the SDK verifies it against the published JWKS pubkey.
    compiled_wasm: Optional[bytes] = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    bundle_manifest: Optional[str] = None  # exact signed JSON bytes, as text
    bundle_signature: Optional[bytes] = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )


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
