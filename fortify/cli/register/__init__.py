"""Agent registration: Python API and `fortify register` CLI."""

from fortify.cli.register.register import register_agent
from fortify.cli.register.manifest import create_manifest
from fortify.cli.register.models import AgentManifest
from fortify.cli.register.main import add_parser, main

__all__ = ["register_agent", "create_manifest", "AgentManifest", "add_parser", "main"]
