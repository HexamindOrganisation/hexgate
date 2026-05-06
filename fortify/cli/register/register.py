import os
import requests
from langchain.tools import BaseTool

from fortify.cli.register.manifest import create_manifest
from fortify.cli.register.models import AgentType

def register_agent(
    agent: AgentType,
    *,
    description: str | None = None,
    tools: list[BaseTool] | None = None,
) -> dict:
    """Create and register an agent manifest to platform /agents/register."""
    manifest = create_manifest(agent, description=description, tools=tools)

    api_key, api_url = os.environ.get("FORTIFY_KEY"), os.environ.get("FORTIFY_API_URL")
    if api_key is None or api_url is None:
        raise ValueError("FORTIFY_KEY and FORTIFY_API_URL must be set")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response = requests.post(
        f"{api_url}/v1/agents/register",
        headers=headers,
        json={"manifest": manifest.model_dump()},
    )
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise ValueError(f"Failed to register agent: {e}")
    return response.json()