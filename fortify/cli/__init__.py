"""Terminal chat UI entrypoints for fortify."""

from __future__ import annotations

import sys


def run() -> None:
    """Dispatch `fortify <subcommand>` or fall through to the chat app."""
    if len(sys.argv) > 1 and sys.argv[1] == "register":
        from fortify.cli.register.main import main as run_register

        sys.exit(run_register(sys.argv[2:]))

    from fortify.cli.app import run as run_app

    run_app()


__all__ = ["run"]
