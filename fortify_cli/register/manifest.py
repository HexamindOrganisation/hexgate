from agents import Agent as OpenAIAgent
from google.adk.agents import Agent as GoogleAgent
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph as LangChainAgent
from pydantic_ai import Agent as PydanticAIAgent

from fortify_cli.register.google import create_google_manifest
from fortify_cli.register.langchain import create_langchain_manifest
from fortify_cli.register.models import AgentManifest
from fortify_cli.register.openai import create_openai_manifest
from fortify_cli.register.pydantic_ai import create_pydantic_ai_manifest

AgentType = OpenAIAgent | GoogleAgent | LangChainAgent | PydanticAIAgent

def create_manifest(
    agent: AgentType,
    *,
    description: str | None = None,
    tools: list[BaseTool] | None = None,
) -> AgentManifest:
    """Create an AgentManifest from an Agent.

    `tools` is required only when `agent` is a LangChain compiled graph,
    since those graphs do not reliably expose their tool nodes.
    """
    if isinstance(agent, OpenAIAgent):
        return create_openai_manifest(agent, description=description)
    if isinstance(agent, GoogleAgent):
        return create_google_manifest(agent, description=description)
    if isinstance(agent, PydanticAIAgent):
        return create_pydantic_ai_manifest(agent, description=description)
    if isinstance(agent, LangChainAgent):
        if tools is None:
            raise ValueError(
                "LangChain graphs require `tools` to be passed explicitly to create_manifest()"
            )
        return create_langchain_manifest(agent, tools, description=description)
    raise ValueError(f"Unsupported agent type: {type(agent)}")
