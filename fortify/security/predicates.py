"""Policy predicates that consume facts extracted from a Biscuit token.

The three evaluators here are the minimum surface M1 needs to demo
user-scoped tool authorization without rewriting the structured policy
engine. Each one raises :class:`PolicyDeniedError` on failure so the
existing GuardedTool flow surfaces it as a normal denial result rather
than a runtime exception.

These predicates intentionally fail-closed when the policy declares a
requirement and the token doesn't carry the matching fact: "you asked
for a user attribution; the token has none" is a deny, not a skip.

When the M2 work lands and policy rules move into native Biscuit Datalog,
these helpers retire — Biscuit's own authorizer handles the same logic
by evaluating in-token checks. They live here so M1 can ship without
that machinery.
"""

from __future__ import annotations

from typing import Any

from fortify.security.errors import PolicyDeniedError

# A facts dict matches what `extract_facts` returns: predicate name to a list
# of single-arity values, in source order, across every Biscuit block.
FactDict = dict[str, list[str | int]]


def _string_facts(facts: FactDict | None, name: str) -> list[str]:
    """Return the string-valued facts under ``name`` (filters out ints)."""
    if not facts:
        return []
    return [v for v in facts.get(name, []) if isinstance(v, str)]


def _int_facts(facts: FactDict | None, name: str) -> list[int]:
    """Return the integer-valued facts under ``name`` (filters out strings)."""
    if not facts:
        return []
    return [v for v in facts.get(name, []) if isinstance(v, int)]


def check_requires_user(
    allowed: list[str] | None,
    facts: FactDict | None,
    tool_name: str,
) -> None:
    """Raise when the policy requires a user fact and none of them match.

    ``allowed`` is the policy's list of acceptable user identifiers. ``None``
    or empty means *no user requirement* — the predicate is a no-op. When a
    requirement exists but the token carries no ``user(...)`` fact at all,
    we also deny: the policy author asked for attribution and got none.
    """
    if not allowed:
        return
    user_facts = _string_facts(facts, "user")
    if not user_facts:
        raise PolicyDeniedError(
            f'Policy on "{tool_name}" requires a user attribution; '
            f"token carries none"
        )
    if not set(user_facts).intersection(allowed):
        raise PolicyDeniedError(
            f'Policy on "{tool_name}" requires user in {sorted(allowed)!r}; '
            f"token presents {sorted(user_facts)!r}"
        )


def check_requires_scope(
    required: list[str] | None,
    facts: FactDict | None,
    tool_name: str,
) -> None:
    """Raise when one or more required scopes is missing from the token.

    All entries in ``required`` must appear in the token's ``scope(...)``
    facts. AND semantics — "this tool needs read AND write" is two scopes.
    """
    if not required:
        return
    present = set(_string_facts(facts, "scope"))
    missing = [s for s in required if s not in present]
    if missing:
        raise PolicyDeniedError(
            f'Policy on "{tool_name}" requires scopes {missing!r}; '
            f"token presents {sorted(present)!r}"
        )


def check_numeric_limit(
    limits: dict[str, str] | None,
    facts: FactDict | None,
    arguments: dict[str, Any] | None,
    tool_name: str,
) -> None:
    """Cap a numeric tool argument by a fact-supplied bound.

    ``limits`` maps an argument name to the fact name that bounds it, e.g.
    ``{"amount": "refund_limit"}`` means *"argument ``amount`` must be
    <= the smallest ``refund_limit(N)`` fact in the token."* The most
    restrictive bound across all matching facts wins (think attenuators
    narrowing a parent cap further).

    Silently skips arguments the caller didn't supply — not our place to
    enforce shape on tools we don't own. But fails closed when the policy
    declares a bound and the token carries no matching fact.
    """
    if not limits:
        return
    arguments = arguments or {}
    for arg_name, fact_name in limits.items():
        if arg_name not in arguments:
            continue
        arg_value = arguments[arg_name]
        if not isinstance(arg_value, (int, float)):
            raise PolicyDeniedError(
                f'Policy on "{tool_name}" caps argument "{arg_name}" via '
                f"{fact_name!r} but the call passed "
                f"{type(arg_value).__name__}"
            )
        bound_values = _int_facts(facts, fact_name)
        if not bound_values:
            raise PolicyDeniedError(
                f'Policy on "{tool_name}" caps argument "{arg_name}" via '
                f"{fact_name!r}; token carries no such fact"
            )
        bound = min(bound_values)
        if arg_value > bound:
            raise PolicyDeniedError(
                f'Policy on "{tool_name}" caps argument "{arg_name}" at '
                f"{bound} via {fact_name!r}; call requested {arg_value}"
            )
