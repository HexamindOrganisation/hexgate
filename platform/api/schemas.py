from datetime import datetime
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# M3 Phase 4 — Organization wire shapes
# ---------------------------------------------------------------------------


class OrgRead(BaseModel):
    """Shared base — what an org looks like over the wire."""

    id: str
    slug: str
    name: str
    created_at: datetime


class OrgWithRole(OrgRead):
    """Org enriched with the caller's role. Returned by ``GET /v1/orgs``
    so the dashboard knows which actions the active user can take in
    each listed org without a second round-trip."""

    role: str  # "owner" | "admin" | "member"


class OrgCreate(BaseModel):
    """``POST /v1/orgs`` body.

    ``slug`` is optional — when omitted, the server derives one from
    ``name`` (sanitised + collision-fallback). Constraints match
    DNS-label rules + a 32-char ceiling so the slug fits comfortably
    in any URL we'd ever render.
    """

    name: str = Field(min_length=1, max_length=64)
    slug: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=32,
        # Lowercase letters, digits, hyphens. Must start with a letter
        # and not end with a hyphen — matches DNS-label conventions so
        # the slug can later double as a hostname/subdomain.
        pattern=r"^[a-z][a-z0-9-]*[a-z0-9]$|^[a-z]$",
    )


class OrgUpdate(BaseModel):
    """``PATCH /v1/orgs/{id}`` body. Both fields optional; omitted
    fields are left unchanged on the row."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    slug: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r"^[a-z][a-z0-9-]*[a-z0-9]$|^[a-z]$",
    )


class TokenMintRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scopes: list[str] = Field(default_factory=lambda: ["mint_user_token", "read_audit"])
    env: str = Field(default="test", pattern="^(test|live)$")


class TokenListItem(BaseModel):
    id: str
    name: str
    masked: str  # e.g., "fty_live_8F3d…k29P"
    scopes: list[str]
    created_at: datetime
    last_used_at: Optional[datetime]


class TokenMintResponse(BaseModel):
    id: str
    name: str
    full: str  # only returned on mint
    masked: str
    scopes: list[str]
    created_at: datetime


class AgentRead(BaseModel):
    id: str
    name: str
    agent_yaml: str
    policy_yaml: str
    system_md: str
    updated_at: datetime
    # Signed WASM bundle compiled from policy_yaml at save time. Null when
    # the platform couldn't compile (opa missing or bad policy) — the SDK
    # then falls back to the pydantic engine. wasm + signature are base64;
    # manifest is the exact signed JSON text (verified over its bytes).
    bundle_wasm_b64: Optional[str] = None
    bundle_manifest: Optional[str] = None
    bundle_signature_b64: Optional[str] = None


class AgentUpdate(BaseModel):
    agent_yaml: str | None = None
    policy_yaml: str | None = None
    system_md: str | None = None


class PolicyValidationError(BaseModel):
    """One diagnostic from the policy-document linter.

    ``role`` is set when the failure was inside a specific entry of a
    role-aware ``policy.yaml``'s ``roles:`` section; ``None`` for errors
    at the top level (e.g. invalid YAML, schema violation).
    """

    role: str | None = None
    line: int | None = None
    message: str


class ValidatePolicyRequest(BaseModel):
    """Body for the policy-document validation endpoint.

    Validates a single ``policy.yaml`` text — either a flat single-policy
    shape or an inline-roles shape with a top-level ``roles:`` map. The
    endpoint runs the same parsing the SDK uses at enforcement time.
    """

    policy_yaml: str


class ValidatePolicyResponse(BaseModel):
    """Result of validating a policy document.

    ``ok`` is True when the document and every nested role parsed cleanly.
    ``errors`` carries per-issue diagnostics.
    """

    ok: bool
    errors: list[PolicyValidationError] = Field(default_factory=list)


# --- Agent manifest registration ---------------------------------------------
# These mirror fortify/cli/register/models.py so SDK and platform stay in sync.


class AgentFramework(StrEnum):
    FORTIFY = "fortify"
    PYDANTIC_AI = "pydantic-ai"
    LANGCHAIN = "langchain"
    GOOGLE = "google"
    OPENAI = "openai"


class InputProperty(BaseModel):
    title: str
    type: str


class InputSchema(BaseModel):
    properties: dict[str, InputProperty]
    required: list[str]


class ToolDefinition(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: InputSchema


class AgentManifest(BaseModel):
    """Schema for the manifest of an agent."""

    name: str
    description: Optional[str] = None
    framework: AgentFramework
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    tools: list[ToolDefinition]


class RegisterAgentRequest(BaseModel):
    manifest: AgentManifest


class RegisterAgentResponse(BaseModel):
    agent_id: str
    agent_version_id: str
    name: str
    version: int
    content_hash: str
    created: bool  # False if the same content_hash already existed (no-op)


class AgentManifestView(BaseModel):
    """Resolved latest manifest of an agent, for the dashboard read path.

    ``manifest`` is None when the Agent row exists but no AgentVersion has
    been registered yet.
    ``name`` lives on the envelope so the picker can display it directly.
    """

    name: str
    manifest: Optional[AgentManifest] = None
    version: Optional[int] = None
    content_hash: Optional[str] = None
    updated_at: datetime
