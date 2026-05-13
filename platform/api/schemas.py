from datetime import datetime
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field


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
    # Empty dict for single-policy agents; populated for role-aware ones.
    roles: dict[str, str] = Field(default_factory=dict)
    updated_at: datetime


class AgentUpdate(BaseModel):
    agent_yaml: str | None = None
    policy_yaml: str | None = None
    system_md: str | None = None
    roles: dict[str, str] | None = None


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
    description: str
    input_schema: InputSchema


class AgentManifest(BaseModel):
    name: str
    description: Optional[str] = None
    framework: AgentFramework
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
