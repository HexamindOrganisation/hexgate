"""Run one copilot message from the terminal, as a Claimdesk user, without the web app.

    uv run python -m claimdesk.smoke lea "What's the status of CLM-2041?"

Loads agent.py the same way the web app does and calls
`run_copilot(conversation, user, approve)`. An approval request is answered in
the terminal. Uses the same .env files as the app.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from claimdesk import server

USAGE_EXIT = 2


async def main(username: str, message: str) -> int:
    entry = server.load_users().get(username)
    if entry is None:
        print(f"no user {username!r} in users.yaml", file=sys.stderr)
        return USAGE_EXIT
    user = entry[0]

    async def approve(decision: Any) -> bool:
        answer = await asyncio.to_thread(
            input, f"  approve {decision.tool_name}({decision.arguments})? [y/N] "
        )
        return answer.strip().lower().startswith("y")

    mod = server.load_agent()
    print(f"agent.py loaded, protected by hexgate: {server.is_protected(mod)}")
    print(f"running as {user.username} (roles: {', '.join(user.roles) or 'none'})")
    result = await mod.run_copilot(
        [{"role": "user", "content": message}], user, approve
    )
    async for ev in result.stream_events():
        if ev.type == "run_item_stream_event" and ev.name == "tool_called":
            raw = ev.item.raw_item
            print(f"  → {getattr(raw, 'name', '?')}({getattr(raw, 'arguments', '')})")
        elif ev.type == "run_item_stream_event" and ev.name == "tool_output":
            status, reason = server._classify(ev.item.output)
            print(f"    {status}" + (f": {reason}" if reason else ""))
    print(f"reply: {result.final_output}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(USAGE_EXIT)
    sys.exit(asyncio.run(main(sys.argv[1], " ".join(sys.argv[2:]))))
