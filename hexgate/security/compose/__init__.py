"""The ``policy.yaml`` authoring front-end.

Parses the position-wildcard grammar (one entry file + imports) and lowers it to
the ``ModuleContent`` lists the existing linker fold consumes — so the entire
fold → rego → wasm → signing path is reused unchanged. See
:mod:`hexgate.security.compose.grammar` for the shape and
:func:`hexgate.security.compose.resolve.resolve_file` for the entry point.
"""

from __future__ import annotations

from hexgate.security.compose.grammar import Entry
from hexgate.security.compose.parse import parse_entry
from hexgate.security.compose.resolve import resolve_entry, resolve_file, resolve_text

__all__ = ["Entry", "parse_entry", "resolve_entry", "resolve_file", "resolve_text"]
