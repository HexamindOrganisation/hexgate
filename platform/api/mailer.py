"""Outbound email — the seam between FastAPI Users' lifecycle hooks
(``on_after_register`` / ``on_after_request_verify`` /
``on_after_forgot_password``) and however the platform actually ships
mail at deploy time.

The contract is one method (``send(to, subject, body)``) so swapping a
real provider (Resend, Postmark, SES, SMTP) for the dev-mode
:class:`StderrEmailSender` is a one-line change in the factory. Tests
swap in a list-capturing sender via :func:`set_email_sender` so they
can read the tokens that would have been mailed without parsing
stderr.
"""

from __future__ import annotations

import logging
import sys
from typing import Protocol

logger = logging.getLogger("hexgate.platform.email")


class EmailSender(Protocol):
    """Anything with an ``async send(to, subject, body)``.

    Production wires a Resend / Postmark / SMTP implementation that
    actually delivers; the seam stops the UserManager from caring
    about the difference. Methods are async so a future implementation
    can ``await`` the provider's HTTP call without changing callers.
    """

    async def send(self, *, to: str, subject: str, body: str) -> None: ...


class StderrEmailSender:
    """Default dev-mode sender — print the email to stderr.

    The whole point of FastAPI Users' verification + reset flows is the
    magic link inside the body. Printing to stderr lets a dev see the
    link, copy it, and exercise the flow end-to-end without an SMTP
    server set up. Production swaps this for a real provider via
    :func:`set_email_sender` at boot.
    """

    async def send(self, *, to: str, subject: str, body: str) -> None:
        bar = "-" * 60
        print(
            f"\n[email] {bar}\n"
            f"        to:      {to}\n"
            f"        subject: {subject}\n"
            f"        body:\n{_indent(body, '          ')}\n"
            f"        {bar}\n",
            file=sys.stderr,
            flush=True,
        )


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# Module-level singleton so the UserManager hooks (synchronous code path
# inside fastapi-users) can reach the sender without an extra dependency
# injection wire. Tests swap it via :func:`set_email_sender`; production
# swaps it once at boot via the same call before the lifespan finishes.
_current_sender: EmailSender = StderrEmailSender()


def get_email_sender() -> EmailSender:
    """Return the currently configured sender (used by auth.UserManager)."""
    return _current_sender


def set_email_sender(sender: EmailSender) -> None:
    """Replace the currently configured sender.

    Production boot calls this with a real provider implementation;
    tests call it with a capturing fake. There's no provider registry
    yet — when we wire Resend in production we'll do it in main.py's
    lifespan.
    """
    global _current_sender
    _current_sender = sender
