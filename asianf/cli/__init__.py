"""Terminal chat UI entrypoints for asianf."""

from __future__ import annotations


def run() -> None:
    """Launch the terminal chat application lazily."""
    from asianf.cli.app import run as run_app

    run_app()


__all__ = ["run"]
