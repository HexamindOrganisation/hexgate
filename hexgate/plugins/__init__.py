"""Official guards, ready to drop into ``guards=[...]``.

Four plugins built on one shared secret detector (:mod:`hexgate.plugins.secrets`),
covering the two outbound cases and the two inbound ones:

- :data:`secret_guard` — before-guard that **refuses** a call whose arguments
  carry a credential, with an actionable, value-free reason.
- :data:`secret_redactor` — before-guard that **strips** the credential from the
  arguments and lets the cleaned call run.
- :data:`secret_watch` — after-guard (observe) that **flags** a credential that
  leaked into a tool's result, leaving the result untouched.
- :data:`secret_scrubber` — after-guard that **strips** the credential from the
  tool's result, so the cleaned result is what reaches the model and the user.

``secret_watch`` and ``secret_scrubber`` are the inbound pair: watch when you only
want the leak flagged on the operator channel, scrub when the result must be made
safe before it flows on.

``secret_guard`` and ``secret_redactor`` are the two halves of the outbound case;
pick per tool by whether a secret's presence means the call is wrong (guard) or
merely incidental and safe to strip (redactor). Do not register both on the same
tool — the redactor would clean the args before the guard ever sees them.

::

    from hexgate import create_agent
    from hexgate.plugins import secret_guard, secret_watch

    agent, _ = create_agent(model=..., tools=[...], guards=[secret_guard, secret_watch])

The detector primitives are exported too, for building a custom guard (scoped with
``@before_tool(tool_names=[...])``, or with your own reason).
"""

from __future__ import annotations

import logging

from hexgate.guards import (
    Halt,
    Modification,
    Proceed,
    ToolCall,
    ToolOutcome,
    after_tool,
    before_tool,
)
from hexgate.plugins.secrets import (
    SecretHit,
    redact_secrets,
    safe_detail,
    safe_reason,
    scan_secrets,
)

_log = logging.getLogger("hexgate.plugins.secrets")


@before_tool
def secret_guard(call: ToolCall) -> Halt | None:
    """Refuse a tool call whose arguments carry a credential.

    Fail-closed (a raise denies the call). The model sees only the category and
    field, never the value, so the refusal cannot leak and does not hand the
    model a substring to obfuscate and resend.
    """
    hits = scan_secrets(call.args)
    if not hits:
        return None
    return Halt(reason=safe_reason(hits), detail=safe_detail(hits))


@before_tool
def secret_redactor(call: ToolCall) -> Proceed | None:
    """Strip every credential from the arguments and let the cleaned call run.

    Args are JSON, so the strip is a clean recursive walk; the secret leaf becomes
    a ``[REDACTED:<category>]`` marker. Records a :class:`Modification` naming the
    count and categories (never the value) so the rewrite is visible to the trail.
    """
    cleaned, hits = redact_secrets(call.args)
    if not hits:
        return None
    cats = ", ".join(sorted({h.category for h in hits}))
    return Proceed(
        args=cleaned,
        modification=Modification(
            plugin="secret_redactor",
            target="args",
            summary=f"redacted {len(hits)} secret(s): {cats}",
        ),
    )


@after_tool(observe=True)
def secret_watch(call: ToolCall, outcome: ToolOutcome) -> None:
    """Flag a credential that leaked into a tool's result.

    Observe-only (fail-open, cannot halt or rewrite): it logs a value-free warning
    on the operator channel and leaves the result untouched. Scans JSON-ish results
    only; an opaque return object is skipped. Reach for :data:`secret_scrubber`
    instead when the result must be made safe, not merely flagged.

    It walks the full result on every call, so for a high-throughput tool that
    returns large payloads, register a scoped variant rather than this global one::

        after_tool(tool_names=["search"], observe=True)(secret_watch.fn)
    """
    if not outcome.ok:
        return None
    hits = scan_secrets(outcome.value)
    if hits:
        _log.warning(
            "secret_watch: %d probable secret(s) in %r result [%s]",
            len(hits),
            call.tool_name,
            safe_detail(hits),
        )
    return None


@after_tool
def secret_scrubber(call: ToolCall, outcome: ToolOutcome) -> Proceed | None:
    """Strip every credential from a tool's result and let the cleaned result flow.

    The inbound counterpart to :data:`secret_redactor`: where a read/search tool
    returns a payload that carries a credential, this replaces the secret leaf with
    a ``[REDACTED:<category>]`` marker so it never reaches the model or the user.
    Records a value-free :class:`Modification` (count + categories) so the rewrite
    is visible to the trail. A failed call has no result to scrub, so it is left
    for the error to surface untouched.

    Like :data:`secret_watch` it walks the whole result, so scope it to the tools
    that return untrusted payloads rather than registering it globally::

        after_tool(tool_names=["search"])(secret_scrubber.fn)
    """
    if not outcome.ok:
        return None
    cleaned, hits = redact_secrets(outcome.value)
    if not hits:
        return None
    cats = ", ".join(sorted({h.category for h in hits}))
    return Proceed(
        result=cleaned,
        modification=Modification(
            plugin="secret_scrubber",
            target="result",
            summary=f"redacted {len(hits)} secret(s): {cats}",
        ),
    )


__all__ = [
    "SecretHit",
    "redact_secrets",
    "safe_detail",
    "safe_reason",
    "scan_secrets",
    "secret_guard",
    "secret_redactor",
    "secret_scrubber",
    "secret_watch",
]
