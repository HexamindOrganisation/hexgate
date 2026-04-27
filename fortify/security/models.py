"""Pydantic models for agent security policies."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PolicyMode = Literal["allow", "deny", "approval_required"]


class BaseToolPolicy(BaseModel):
    """Define the access mode for a single tool."""

    mode: PolicyMode = "deny"


class FileScope(BaseModel):
    """Restrict a file-oriented tool to explicit path patterns."""

    allowed_paths: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)


class FileToolPolicy(BaseToolPolicy):
    """Define access policy for file-oriented tools."""

    file_scope: FileScope | None = None


ToolPolicy = BaseToolPolicy | FileToolPolicy


class AgentPolicy(BaseModel):
    """Define an agent-wide tool authorization policy."""

    version: int = 1
    default_policy: BaseToolPolicy = Field(default_factory=BaseToolPolicy)
    tools: dict[str, ToolPolicy] = Field(default_factory=dict)
