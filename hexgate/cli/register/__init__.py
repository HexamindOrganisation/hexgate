"""The `hexgate register` CLI + platform-registration API.

Manifest models and the builder now live in :mod:`hexgate.manifest`;
this package only carries platform registration (`register_agent`,
`post_manifest`) and the CLI entry point.
"""

from hexgate.cli.register.main import add_parser, main
from hexgate.cli.register.register import (
    AgentTreeCollision,
    register_agent,
    register_tree,
)

__all__ = [
    "AgentTreeCollision",
    "register_agent",
    "register_tree",
    "add_parser",
    "main",
]
