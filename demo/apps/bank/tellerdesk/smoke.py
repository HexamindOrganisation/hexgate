"""Run one assistant message from the terminal, as a Tellerdesk user, without the web app.

    uv run python -m tellerdesk.smoke camille "What's the balance on ACC-1042?" [C-1042]

The optional last argument is the customer file to open (default: C-1042,
Marc Dubois). Loads agent.py the same way the web app does and calls
`run_assistant(conversation, user, approve)` with the user's attributes plus
that file's. An approval request is answered in the terminal. Uses the same
.env files as the app.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from tellerdesk import bank, server

USAGE_EXIT = 2
MIN_ARGS = 3


async def main(username: str, message: str, customer_id: str) -> int:
    entry = server.load_users().get(username)
    if entry is None:
        print(f"no user {username!r} in users.yaml", file=sys.stderr)
        return USAGE_EXIT
    if not bank.is_customer(customer_id):
        print(f"no customer {customer_id!r}", file=sys.stderr)
        return USAGE_EXIT
    user = server.with_open_file(entry[0], customer_id)

    async def approve(decision: Any) -> bool:
        answer = await asyncio.to_thread(
            input, f"  approve {decision.tool_name}({decision.arguments})? [y/N] "
        )
        return answer.strip().lower().startswith("y")

    mod = server.load_agent()
    print(f"agent.py loaded, protected by hexgate: {server.is_protected(mod)}")
    print(f"running as {user.username} (roles: {', '.join(user.roles) or 'none'})")
    print(f"customer file open: {customer_id}")
    bank.actor.set(user.username)
    bank.open_customer.set(customer_id)
    result = await mod.run_assistant(
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


def _parse(argv: list[str]) -> tuple[str, str, str]:
    """username, message, customer id: the last argument when it names a customer."""
    username, rest = argv[0], argv[1:]
    customer_id = bank.DEFAULT_CUSTOMER
    if len(rest) > 1 and bank.is_customer(rest[-1]):
        customer_id, rest = rest[-1], rest[:-1]
    return username, " ".join(rest), customer_id


if __name__ == "__main__":
    if len(sys.argv) < MIN_ARGS:
        print(__doc__)
        sys.exit(USAGE_EXIT)
    sys.exit(asyncio.run(main(*_parse(sys.argv[1:]))))
