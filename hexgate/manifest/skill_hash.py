"""The skill ``content_hash`` shared by every adapter, so one SKILL.md hashes alike."""

from __future__ import annotations

from hashlib import sha256

_FRONTMATTER_DELIMITER = "---"
_FRONTMATTER_PARTS = 3


def skill_md_body(content: str) -> str:
    """The instructions after a SKILL.md's frontmatter, split the way ADK's parser does.

    Content without a closed frontmatter block is its own body.
    """
    if not content.startswith(_FRONTMATTER_DELIMITER):
        return content.strip()
    parts = content.split(_FRONTMATTER_DELIMITER, _FRONTMATTER_PARTS - 1)
    if len(parts) < _FRONTMATTER_PARTS:
        return content.strip()
    return parts[-1].strip()


def skill_content_hash(body: str) -> str:
    """sha256 hexdigest of a skill's instruction body."""
    return sha256(body.encode("utf-8")).hexdigest()
