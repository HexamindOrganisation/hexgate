"""Agent registration: Python API and `hexgate register` CLI."""

from hexgate.cli.register.register import register_agent
from hexgate.cli.register.manifest import create_manifest
from hexgate.cli.register.models import AgentManifest
from hexgate.cli.register.main import add_parser, main

__all__ = ["register_agent", "create_manifest", "AgentManifest", "add_parser", "main"]
