"""CLI entrypoint for `fortify register`."""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import TYPE_CHECKING, Any

from fortify.cli.register.register import register_agent

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool


def _load_spec(spec: str) -> Any:
    """Resolve a `module.path:attr` spec to its target object."""
    module_path, sep, attr = spec.partition(":")
    if not sep or not module_path or not attr:
        raise ValueError(
            f"Invalid spec {spec!r}: expected 'module.path:attr' (e.g. my_app.module:my_attr)"
        )

    if "" not in sys.path:
        sys.path.insert(0, "")

    module = importlib.import_module(module_path)
    try:
        return getattr(module, attr)
    except AttributeError as e:
        raise AttributeError(f"Module {module_path!r} has no attribute {attr!r}") from e


def _load_agent(spec: str) -> Any:
    """Resolve an agent from a `module.path:attr` spec."""
    return _load_spec(spec)


def _load_tools(spec: str) -> list[BaseTool]:
    """Resolve a list of LangChain BaseTools from a `module.path:attr` spec."""
    from langchain_core.tools import BaseTool

    tools = _load_spec(spec)
    if not isinstance(tools, list) or not all(isinstance(t, BaseTool) for t in tools):
        raise TypeError(
            f"Expected {spec!r} to resolve to a list of langchain BaseTool instances"
        )
    return tools


def main(argv: list[str]) -> None:
    """Register an agent to the Fortify platform."""
    parser = argparse.ArgumentParser(
        prog="fortify register",
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

    args = parser.parse_args(argv)

    agent = _load_agent(args.agent)
    tools = _load_tools(args.tools) if args.tools is not None else None

    register_agent(agent, description=args.description, tools=tools)
