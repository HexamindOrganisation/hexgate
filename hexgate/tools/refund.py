"""Demo refund tool — a stub the support-bot playground uses to exercise role policies.

Doesn't actually move money. Returns a synthetic confirmation so the playground
can show the policy gate firing before the tool runs, and the role-aware
constraints can fail vs succeed visibly per role.

TODO(M2): relocate to ``examples/`` once demo agents move out of
``BUILTIN_TOOLS`` and the platform's ``support_bot`` seed; this tool only
exists as a builtin to keep the seeded support_bot YAML resolvable today.
"""

from __future__ import annotations

import uuid

from hexgate.tools.decorators import agent_tool


def _format_refund_call(arguments: dict[str, object]) -> str:
    """Compact label for the tool stream — what the dashboard shows mid-call."""
    customer = arguments.get("customer_id") or "?"
    amount = arguments.get("amount") or "?"
    currency = arguments.get("currency") or ""
    return f"refunding {amount} {currency} to {customer}".strip()


@agent_tool(
    name="refund_order",
    call_formatter=_format_refund_call,
    failure_mode="result",
)
async def refund_order(
    customer_id: str,
    amount: int,
    currency: str = "USD",
    reason: str | None = None,
) -> dict:
    """Refund an order for a customer.

    Demo-only stub: returns a synthetic confirmation so the Playground can
    show the policy / constraint gate firing before any real side-effect.
    The agent's role policy is what decides whether this call is allowed —
    in production this would dispatch to the dev's billing service.
    """
    return {
        "ok": True,
        "refund_id": f"rf_{uuid.uuid4().hex[:8]}",
        "customer_id": customer_id,
        "amount": amount,
        "currency": currency,
        "reason": reason,
        "note": "demo stub — no money moved",
    }
