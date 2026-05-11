"""Attenuate a verified Fortify token with user-scoped facts and checks.

In production the dev's backend calls :func:`attenuate_for_user` on each
inbound request, taking the project-wide token from ``FORTIFY_KEY`` and
appending a per-user block before forwarding to the agent runner. The
new envelope still chains to the platform's root public key — biscuit's
``append`` protocol handles the signature linkage with an ephemeral
keypair, so the dev never holds the platform's private key.

For the Playground demo, the dev's local ``fortify --serve`` process plays
the same role: it receives the "act as alice" metadata over the WebSocket
from the dashboard and runs the attenuation in-process before invoking
the agent. Same code path, same chain, different trigger.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fortify.cloud.biscuit import (
    TokenError,
    TokenSignatureError,
    parse_envelope,
)

# Datalog identifier rule: lowercase head, then alnum + underscore. Matches
# what ``extract_facts`` parses, so attenuation never mints a fact name that
# the SDK can't read back.
_IDENT_RE = re.compile(r"^[a-z][a-zA-Z0-9_]*$")


def _escape_datalog_string(value: str) -> str:
    """Escape ``value`` for safe embedding inside a Biscuit string literal.

    Backslashes and double-quotes are the only metacharacters Biscuit's
    Datalog lexer treats specially inside ``"..."``. Escaping them prevents
    a user-supplied identifier from breaking out of the quote and injecting
    extra facts or checks into the attenuation block.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def attenuate_for_user(
    parent_envelope: str,
    public_key_bytes: bytes,
    *,
    user: str,
    scope: list[str] | None = None,
    limits: dict[str, int] | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Return a new envelope with a user-attribution block appended.

    Verifies ``parent_envelope`` against ``public_key_bytes`` first, then
    appends a Biscuit block carrying:

    * ``user("...")``  — the authenticated user id.
    * ``scope("...")`` facts — one per entry in ``scope``.
    * ``name(N)`` facts — one per ``(name, N)`` pair in ``limits`` (e.g.
      ``refund_limit(50)``). Names must match ``[a-z][a-zA-Z0-9_]*``.
    * ``check if time($t), $t < <now+ttl_seconds>`` when ``ttl_seconds`` is
      set, narrowing the parent's TTL (or adding one if the parent had none).

    The resulting envelope keeps the ``fty_<env>_<project>_<...>`` wire
    format unchanged — only the biscuit payload grows by one block.

    Raises:
        TokenError: malformed envelope, invalid limit name, non-integer
            limit value.
        TokenSignatureError: malformed public key, or parent biscuit fails
            to verify (tampered / wrong key / corrupt payload).
    """
    from biscuit_auth import (
        Algorithm,
        Biscuit,
        BiscuitValidationError,
        BlockBuilder,
        PublicKey,
    )

    env, project_id, biscuit_b64 = parse_envelope(parent_envelope)

    try:
        pub = PublicKey.from_bytes(public_key_bytes, Algorithm.Ed25519)
    except (ValueError, TypeError) as exc:
        raise TokenSignatureError(f"malformed public key: {exc}") from exc
    try:
        parent = Biscuit.from_base64(biscuit_b64, pub)
    except BiscuitValidationError as exc:
        raise TokenSignatureError(str(exc)) from exc

    source_lines: list[str] = [f'user("{_escape_datalog_string(user)}");']
    for scope_value in scope or []:
        source_lines.append(f'scope("{_escape_datalog_string(scope_value)}");')
    for name, value in (limits or {}).items():
        if not _IDENT_RE.match(name):
            raise TokenError(
                f"invalid limit name {name!r}; must match {_IDENT_RE.pattern}"
            )
        if isinstance(value, bool) or not isinstance(value, int):
            raise TokenError(
                f"limit value for {name!r} must be int, got "
                f"{type(value).__name__}"
            )
        source_lines.append(f"{name}({value});")
    if ttl_seconds is not None:
        if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise TokenError(
                f"ttl_seconds must be a positive int, got {ttl_seconds!r}"
            )
        expiry = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        # Biscuit's Datalog parses ISO-8601 datetimes; second-resolution is
        # plenty for capability expiry (tokens that need finer windows would
        # use a different primitive).
        source_lines.append(
            f"check if time($t), $t < {expiry.strftime('%Y-%m-%dT%H:%M:%SZ')};"
        )

    block = BlockBuilder("\n".join(source_lines))
    child = parent.append(block)
    return f"fty_{env}_{project_id}_{child.to_base64()}"
