import os
import requests
from dotenv import load_dotenv
from langchain.tools import BaseTool

from fortify.cli.register.manifest import create_manifest
from fortify.cli.register.models import AgentType

DEFAULT_API_URL = "http://localhost:8000"


def register_agent(
    agent: AgentType,
    *,
    description: str | None = None,
    tools: list[BaseTool] | None = None,
) -> dict:
    """Create and register an agent manifest to platform /agents."""
    load_dotenv()
    manifest = create_manifest(agent, description=description, tools=tools)

    api_key = os.environ.get("FORTIFY_KEY")
    if api_key is None:
        raise ValueError("FORTIFY_KEY must be set")
    api_url = os.environ.get("FORTIFY_API_URL", DEFAULT_API_URL)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response = requests.post(
        f"{api_url}/v1/agents",
        headers=headers,
        json={"manifest": manifest.model_dump()},
    )
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise ValueError(f"Failed to register agent: {e}")
    return response.json()
