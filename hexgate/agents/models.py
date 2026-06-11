"""Pydantic models for packaged agent definitions."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AgentSpec(BaseModel):
    """Define a builtin agent specification."""

    name: str
    model: str
    system_prompt: str
    tools: list[str] = Field(default_factory=list)
    policy: str
    subagents: list[str] = Field(default_factory=list)
    handoffs: list[str] = Field(default_factory=list)
