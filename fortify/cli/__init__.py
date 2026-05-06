"""Terminal chat UI entrypoints for fortify."""

from __future__ import annotations

import sys


def run() -> None:
    """Dispatch `fortify <subcommand>` or fall through to the chat app."""
    arguments = sys.argv
    if len(arguments) > 1 and arguments[1] == "register":
        from fortify.cli.register.main import main as run_register

        run_register(arguments[2:])
        return

    from fortify.cli.app import run as run_app

    run_app()


__all__ = ["run"]
