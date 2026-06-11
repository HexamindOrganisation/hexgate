"""Top-level dispatcher for the `hexgate` CLI.

Usage:
    hexgate chat [--agent ...] [--model ...] [--use ...] [--list-agents] [--approval-mode ...]
    hexgate serve [--agent ...] [--model ...] [--use ...] [--approval-mode ...]
    hexgate register --agent module.path:attr [--description ...] [--tools module.path:attr]
    hexgate policy {build,validate,show-rego,test} <source.yaml> [...]
"""

from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser and register every subcommand."""
    parser = argparse.ArgumentParser(
        prog="hexgate",
        description="Authorization infrastructure for AI agents.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")

    from hexgate.cli import chat, policy, register, serve

    chat.add_parser(subparsers)
    serve.add_parser(subparsers)
    register.add_parser(subparsers)
    policy.add_parser(subparsers)

    return parser


def run() -> None:
    """Dispatch one of `hexgate {chat,serve,register,policy} ...`."""
    parser = _build_parser()
    args = parser.parse_args()
    exit_code = args.func(args) or 0
    sys.exit(exit_code)


__all__ = ["run"]
