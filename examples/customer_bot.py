"""Canonical end-to-end demo: a customer-support agent driven through
the Fortify dashboard.

Built with ``fortify.create_agent()`` so the returned ``FortifyAgent``
carries its name, tools, model, and system prompt as plain attributes.
The manifest builder reads everything off the object — no
``--tools`` / ``--system-prompt`` / ``--model`` flags needed when
registering.

Workflow
--------
1. Run platform API + dashboard, sign in, create/select a project, mint a
   token. Add it to ``asianf/.env`` as ``FORTIFY_KEY=fty_live_...``.

2. Register this agent — the manifest is derived in one call from the
   FortifyAgent object, and the platform auto-generates the starter
   role-aware policy:

   .. code-block:: bash

      uv run fortify register --agent examples.customer_bot:agent

   ✓ Look at the dashboard → /agents → ``customer_bot`` is listed with
     its manifest (description, model, tools, system prompt).
   ✓ Open /policies → the editor shows the four-role generated YAML
     plus a heads-up comment naming ``refund_customer`` and
     ``lookup_product`` (the heuristic couldn't classify them — they
     land in the write bucket, fail-closed).

3. Serve the agent so the dashboard Playground can drive it:

   .. code-block:: bash

      FORTIFY_AGENT_NAME=customer_bot uv run fortify serve

   (Until the uvicorn-style serve lands, the agent name has to be
   set via the env var. After that this becomes
   ``fortify serve examples.customer_bot:agent``.)

4. Open /playground in the dashboard, pick a role ("Acting as: admin /
   member / default"), and send a message that touches each tool. Watch
   the Decisions sidebar — admin's writes pass through; member's
   writes trigger approval prompts; shells gate even for admin.

5. Edit the policy in /policies → save → the SDK's next turn picks up
   the change via the ETag conditional GET. Demote ``admin``'s
   ``update_order_status`` to ``approval_required``, send a follow-up
   message, and watch admin now hit the approval gate too.

Tool layout — exercises every branch of the classifier
------------------------------------------------------
- ``web_search``      — read-shape (substring ``search``)
- ``read_customer``   — read-shape (prefix ``read_``)
- ``create_ticket``   — write-shape (prefix ``create_``)
- ``update_order_status`` — write-shape (prefix ``update_``)
- ``bash_safe``       — shell-shape (substring ``bash``)
- ``refund_customer`` — unknown (custom business logic; fail-closed)
- ``lookup_product``  — unknown (``lookup_`` isn't in the read patterns
                                  on purpose — too easy to false-positive)
"""

from __future__ import annotations

import asyncio

from langchain_core.tools import tool

from fortify import create_agent
from fortify.runtime import User


# ---------------------------------------------------------------------------
# Tools — names chosen to exercise every branch of _classify_tool().
# Stubs in place of real implementations; replace the bodies when the
# policy + roles round-trip looks right in the dashboard.
# ---------------------------------------------------------------------------


@tool
def web_search(query: str) -> str:
    """Search the public web for `query` and return a one-paragraph summary."""
    return (
        f"(stub) top result for '{query}': the open web is a wide and "
        f"varied place. Wire up your real search backend here."
    )


@tool
def read_customer(customer_id: str) -> str:
    """Return the profile + contact details for `customer_id`."""
    return (
        f"(stub) customer {customer_id}: email=alice@example.com, "
        f"plan=enterprise, lifetime_value=$12,400."
    )


@tool
def create_ticket(customer_id: str, subject: str, body: str) -> str:
    """File a new support ticket against `customer_id`.

    The ``create_`` prefix lands this in the write bucket — admin can
    invoke it directly; member needs approval.
    """
    return f"(stub) opened ticket TKT-1234 for {customer_id}: {subject}"


@tool
def update_order_status(order_id: str, status: str) -> str:
    """Set the status (`shipped` | `cancelled` | `refunded`) of `order_id`."""
    return f"(stub) order {order_id} → {status}"


@tool
def bash_safe(script: str) -> str:
    """Run a short shell snippet inside a sandbox.

    The ``bash`` substring pins this in the shell bucket — both
    member AND admin require approval even after edits, because
    shells are the highest blast-radius primitive.
    """
    return f"(stub) executed: {script[:80]}"


@tool
def refund_customer(order_id: str, amount: float, currency: str = "USD") -> str:
    """Issue a refund against `order_id`.

    The heuristic doesn't recognise ``refund_*`` as read / write / shell —
    lands in "unknown". The generated policy surfaces this in a
    heads-up comment so the operator reclassifies it deliberately in
    the dashboard (probably approval_required for member, allow for
    admin — or for production: explicit ``constraints`` on amount).
    """
    return f"(stub) refunded {amount} {currency} on {order_id}"


@tool
def lookup_product(sku: str) -> str:
    """Fetch product details by SKU.

    ``lookup_*`` is deliberately not in the read patterns to avoid
    false-positives. Lands in "unknown" alongside ``refund_customer``.
    """
    return f"(stub) SKU {sku}: 'Widget Mk II', $29.95, 142 in stock"


# ---------------------------------------------------------------------------
# The FortifyAgent. create_agent() returns (FortifyAgent, CallbackHandler);
# we expose the agent as ``agent`` for ``fortify register --agent
# examples.customer_bot:agent``. The FortifyAgent's .name, .tools,
# .model, and .system_prompt fields are read directly by
# create_fortify_manifest — no need to repeat them on the CLI.
# ---------------------------------------------------------------------------


agent, _handler = create_agent(
    model="gpt-4o-mini",
    tools=[
        web_search,
        read_customer,
        create_ticket,
        update_order_status,
        bash_safe,
        refund_customer,
        lookup_product,
    ],
    system_prompt=(
        "You are a customer support agent. Help customers with their "
        "orders, refunds, and account questions. Always confirm details "
        "before invoking writes or shell tools."
    ),
    name="customer_bot",
)


# ---------------------------------------------------------------------------
# Optional: local smoke without going through the platform. Confirms the
# tools wire up + the agent runs before the register/serve cycle.
#   uv run python examples/customer_bot.py
# ---------------------------------------------------------------------------


async def _local_smoke() -> None:
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Look up customer C-42."}]},
        config={"configurable": {"thread_id": "demo-thread-1"}},
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(_local_smoke())
