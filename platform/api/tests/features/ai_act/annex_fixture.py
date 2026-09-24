"""The annex the renderer is tested against: PR 3's golden file, not a copy.

This module used to hold a hand-written dict in a shape the renderer invented
while PR 3 was unmerged. Two schemas for one artifact is how the renderer came
to read ``cover["project_name"]`` from an annex that stores
``cover["project"]["name"]``, with every test still green. There is one annex
shape, and ``golden_annex.json`` — written and asserted by the assembler's own
tests — is it.

The golden file already carries the awkward cases the render tests need: an
incomplete inventory entry (its matrix is annex-only), an agent whose matrix
could not be derived, an empty sample table, and redacted argument snapshots.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from hexgate_api.features.ai_act import copy as report_copy

_GOLDEN = Path(__file__).with_name("golden_annex.json")
_MARKER = re.compile(r"^<copy:([A-Z_]+)>$")


def _rehydrate(value: Any) -> Any:
    """Put the fixed sentences back where the golden file holds a marker.

    The assembler's tests replace each fixed sentence with ``<copy:NAME>`` so
    the golden file pins the annex's shape rather than its prose. The renderer
    wants the prose back: a document that lays out "<copy:TITLE>" is not the
    document that ships, and its line breaks and column widths are not the
    ones a reader will see.
    """
    if isinstance(value, dict):
        return {key: _rehydrate(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rehydrate(item) for item in value]
    if isinstance(value, str) and (match := _MARKER.match(value)):
        return getattr(report_copy, match.group(1))
    return value


SAMPLE_ANNEX: dict = _rehydrate(json.loads(_GOLDEN.read_text()))


def sample_annex() -> dict:
    """A fresh deep copy, so a test that mutates one can't affect the next."""
    return copy.deepcopy(SAMPLE_ANNEX)
