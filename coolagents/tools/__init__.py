"""Tool definitions."""

from coolagents.tools.decorators import agent_tool
from coolagents.tools.fetch import fetch
from coolagents.tools.websearch import web_search

__all__ = ["agent_tool", "fetch", "web_search"]
