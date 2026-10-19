"""Plain (non-fixture) helpers shared by the native agent tests."""

from __future__ import annotations

from hexgate.agents.factory import HexgateAgent


async def run_once(agent: HexgateAgent, method: str) -> None:
    """Drive one run through the native boundary named by ``method``."""
    if method == "ainvoke":
        await agent.ainvoke({"messages": []}, config={})
        return
    async for _ in agent.astream_events({"messages": []}, config={}, version="v2"):
        pass
