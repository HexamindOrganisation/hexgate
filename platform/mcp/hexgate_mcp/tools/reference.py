"""`policy_reference`: the policy docs, so any MCP client can learn the format.

Served from ``docs/policy/*.mdx``, copied into the image at ``/app/docs/policy``;
no API call, so the tool needs no scope.
"""

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from hexgate_mcp.tools import READ_ONLY

logger = logging.getLogger(__name__)

# Sent at `initialize`. Many clients drop that field, so the tool's own
# description repeats it: a tool description always reaches the model.
INSTRUCTIONS = (
    "Call policy_reference before writing a policy, and policy_check before saving one."
)


def find_docs_dir(start: Path = Path(__file__)) -> Path | None:
    """The nearest ``docs/policy`` above `start`: ``/app/docs/policy`` in the
    image, the repo's own ``docs/policy`` in a checkout."""
    for parent in start.resolve().parents:
        candidate = parent / "docs" / "policy"
        if candidate.is_dir():
            return candidate
    return None


def load_sections(docs_dir: Path | None) -> dict[str, str]:
    """Each ``.mdx`` file's text by its stem, which is also the slug of the
    ``/policy/<section>`` links inside the docs."""
    if docs_dir is None:
        return {}
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(docs_dir.glob("*.mdx"))
    }


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}
    fields = {}
    for line in lines[1:]:
        if line == "---":
            break
        name, _, value = line.partition(":")
        fields[name] = value.strip().strip('"')
    return fields


def index_text(sections: dict[str, str]) -> str:
    lines = ["Sections (pass one as `section`):"]
    for name, text in sections.items():
        fields = _frontmatter(text)
        title, description = fields.get("title", ""), fields.get("description", "")
        lines.append(f"- {name}: {title} — {description}")
    return "\n".join(lines)


def register(mcp: FastMCP, sections: dict[str, str] | None = None) -> None:
    if sections is None:
        sections = load_sections(find_docs_dir())
    if not sections:
        # Logged rather than raised: the other tools still work without it.
        logger.warning("no docs/policy/*.mdx found; policy_reference will refuse")
    index = index_text(sections)

    @mcp.tool(
        description=(
            "Read the Hexgate policy reference: the YAML shape, the condition "
            "grammar, and how policies are built, signed and tested. With no "
            "`section`, lists the sections. " + INSTRUCTIONS
        ),
        annotations=READ_ONLY,
        # Unstructured, like every tool here: a `str` return otherwise goes
        # out a second time as structuredContent.
        structured_output=False,
    )
    def policy_reference(section: str | None = None) -> str:
        if not sections:
            raise ToolError("the policy reference is not installed on this server")
        if section is None:
            return index
        # A lookup, never a path: the name cannot reach another file.
        if section not in sections:
            raise ToolError(
                f"unknown section {section!r}; one of: {', '.join(sections)}"
            )
        return sections[section]
