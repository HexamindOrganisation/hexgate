"""Pydantic models for agent security policies."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PolicyMode = Literal["allow", "deny", "approval_required"]


class ToolPolicy(BaseModel):
    """Define the access mode for a single tool."""

    mode: PolicyMode = "deny"


class AgentPolicy(BaseModel):
    """Define an agent-wide tool authorization policy."""

    version: int = 1
    default_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    tools: dict[str, ToolPolicy] = Field(default_factory=dict)
