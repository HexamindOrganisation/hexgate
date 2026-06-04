"""CLI entrypoint for `fortify register`."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

from fortify.cli._common import load_spec
from fortify.cli.register.register import register_agent

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool


def _load_agent(spec: str) -> Any:
    """Resolve an agent from a `module.path:attr` spec."""
    return load_spec(spec)


def _load_tools(spec: str) -> list[BaseTool]:
    """Resolve a list of LangChain BaseTools from a `module.path:attr` spec."""
    from langchain_core.tools import BaseTool

    tools = load_spec(spec)
    if not isinstance(tools, list) or not all(isinstance(t, BaseTool) for t in tools):
        raise TypeError(
            f"Expected {spec!r} to resolve to a list of langchain BaseTool instances"
        )
    return tools


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `register` subcommand on the top-level fortify CLI.

    No env-loading side-effects here — the previous
    ``load_dotenv()`` call leaked env vars into the process whenever
    the top-level parser was built (e.g., from another subcommand's
    test). ``main()`` calls :func:`bootstrap` at invocation time,
    which loads ``.env`` with ``override=False`` so the shell wins.
    """
    parser = subparsers.add_parser(
        "register",
        help="Register an agent to the Fortify platform.",
        description="Register an agent to the Fortify platform.",
    )
    parser.add_argument(
        "--agent",
        required=True,
        help="Agent import path, e.g. my_app.agents:my_agent",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="Optional human-readable description for the agent.",
    )
    parser.add_argument(
        "--tools",
        default=None,
        help="Optional import path to a list of BaseTool, e.g. my_app.tools:my_tools",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Optional model identifier for LangChain graphs (other frameworks "
            "read it off the agent object). Plain string, e.g. 'gpt-4o-mini'."
        ),
    )
    parser.add_argument(
        "--system-prompt",
        dest="system_prompt",
        default=None,
        help=(
            "Optional system prompt for LangChain graphs. Either a literal "
            "string or a path to a .md / .txt / .jinja file (loaded as text)."
        ),
    )
    parser.set_defaults(func=main)


def main(args: argparse.Namespace) -> int:
    """Entrypoint for the `fortify register` subcommand."""
    # Load .env at invocation time (not at parser-build time, which
    # would pollute the env for siblings that don't expect it). The
    # post-Phase-7 dotenv contract is ``override=False`` — shell wins
    # over file — see fortify/bootstrap.py.
    from fortify.bootstrap import bootstrap

    bootstrap()
    agent = _load_agent(args.agent)
    tools = _load_tools(args.tools) if args.tools is not None else None
    system_prompt = (
        _load_system_prompt(args.system_prompt)
        if args.system_prompt is not None
        else None
    )

    register_agent(
        agent,
        description=args.description,
        tools=tools,
        model=args.model,
        system_prompt=system_prompt,
    )
    return 0


def _load_system_prompt(value: str) -> str:
    """Reuse the agent-factory's file-path resolver so .md paths work here too."""
    from fortify.agents.factory import load_system_prompt

    resolved = load_system_prompt(value)
    return resolved if resolved is not None else value
