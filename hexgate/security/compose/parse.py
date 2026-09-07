"""Parse a ``policy.yaml`` document into a grammar :class:`Entry`.

A thin seam over YAML + pydantic: it exists so parse failures surface as a
:class:`~hexgate.security.modules.LinkError` (the error family the resolver and
the platform already catch) with the source file named, rather than a raw
``yaml``/``pydantic`` exception.
"""

from __future__ import annotations

import yaml
from pydantic import ValidationError

from hexgate.security.compose.grammar import Entry
from hexgate.security.modules import LinkError


def parse_entry(text: str, *, source: str = "policy.yaml") -> Entry:
    """Parse one policy document's text into an :class:`Entry`."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LinkError(f"{source}: invalid YAML — {exc}") from exc
    # An empty / comment-only / explicitly-null document is an empty (fail-closed
    # deny-all) policy. Any other non-mapping (a list, bool, number) is malformed
    # — error rather than silently coercing a falsy value to an empty policy.
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise LinkError(
            f"{source}: a policy document must be a mapping, got {type(raw).__name__}"
        )
    try:
        return Entry.model_validate(raw)
    except ValidationError as exc:
        raise LinkError(f"{source}: {exc}") from exc
