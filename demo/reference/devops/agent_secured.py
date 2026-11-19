"""Shipyard ops copilot, built with the OpenAI Agents SDK.

Engineers chat with it in Shipyard to check services, read logs, restart,
scale and deploy. The Shipyard web app (shipyard/server.py) calls
`run_copilot` for every message and streams the result to the browser.
"""

from agents import Agent, function_tool
from hexgate.adapters.openai import HexgateRunner
from hexgate.runtime import HexgateContext

from shipyard import infra


@function_tool
def list_services() -> str:
    """List every service with its version, replicas, health and error rate per environment."""
    return str(infra.list_services())


@function_tool
def get_logs(service: str, env: str) -> str:
    """Read the latest log lines of a service in an environment ("staging" or "prod")."""
    return infra.get_logs(service, env)


@function_tool
def restart_service(service: str, env: str) -> str:
    """Restart a service in an environment ("staging" or "prod")."""
    return infra.restart(service, env)


@function_tool
def deploy_version(service: str, version: str, env: str) -> str:
    """Deploy a version of a service to an environment ("staging" or "prod")."""
    return infra.deploy(service, version, env)


@function_tool
def scale_service(service: str, env: str, replicas: int) -> str:
    """Set the number of replicas of a service in an environment."""
    return infra.scale(service, env, replicas)


@function_tool
def delete_environment(env: str) -> str:
    """Delete a whole environment and every service running in it."""
    return infra.delete_environment(env)


copilot = Agent(
    name="ops_copilot",
    instructions=(
        "You are the Shipyard ops copilot. Engineers ask you to check services, "
        "read logs, restart, scale and deploy. Be efficient: when an engineer "
        "asks you to do something, do it with your tools, then say in one or "
        "two sentences what you did. If a tool reports an error or a refusal, "
        "say so plainly and suggest what could be done instead."
    ),
    tools=[list_services, get_logs, restart_service, deploy_version, scale_service, delete_environment],
    model="gpt-4o-mini",
)


async def run_copilot(conversation, user, approve):
    """Start a streamed copilot run for one chat message.

    conversation: the chat so far, as Agents SDK input items.
    user: the person signed in to Shipyard (user.username, user.name, user.roles).
    approve: an async callback that asks a human in the Shipyard page to
        approve an action; it returns True when they click Approve.
    """
    caller = HexgateContext(user_id=user.username, user_roles=list(user.roles))
    runner = HexgateRunner(approval_handler=approve)
    return await runner.arun_streamed(copilot, conversation, hexgate_context=caller)
