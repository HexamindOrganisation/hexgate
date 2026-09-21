"""Agent manifest: the SDK-side registration contract (models + builder).

Consumed by the SDK public API and by the ``hexgate register`` CLI
(``hexgate.cli.register``), which posts a built manifest to the platform.
"""

from hexgate.manifest.builder import create_manifest, create_manifest_async
from hexgate.manifest.models import (
    AgentFramework,
    AgentManifest,
    InputProperty,
    InputSchema,
    SkillDefinition,
    SkillResources,
    ToolDefinition,
)

__all__ = [
    "AgentFramework",
    "AgentManifest",
    "InputProperty",
    "InputSchema",
    "SkillDefinition",
    "SkillResources",
    "ToolDefinition",
    "create_manifest",
    "create_manifest_async",
]
