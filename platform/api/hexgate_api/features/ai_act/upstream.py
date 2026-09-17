"""The two interfaces this slice consumes from PRs that land beside it.

Both are specified in ``Specs/ai_act_evidence_report.md`` and owned by another
PR: ``AgentClassification`` by PR 1 (agent AI Act classification) and
``authorisation_matrix`` by PR 2 (authorisation matrix). Neither is reproduced
here — this module only resolves them, so the slice imports and tests cleanly
whether or not they have merged yet, and picks them up unchanged once they do.

The degraded readings are the truthful ones, not placeholders:

* no ``AgentClassification`` table means no operator has recorded an entry, so
  every agent reads as **incomplete** — which is exactly how PR 1 defines an
  agent with no row.
* no ``authorisation_matrix`` means section 2 states that no matrix could be
  derived, with the reason. It never falls back to a matrix of its own: a
  second implementation of deny-by-default is the one thing a control
  statement must not be built on.
"""

from __future__ import annotations

from typing import Any, Callable


def classification_model() -> type | None:
    """PR 1's ``AgentClassification`` table, or None before it lands."""
    try:
        from hexgate_api.models import AgentClassification
    except ImportError:
        return None
    return AgentClassification


def authorisation_matrix_fn() -> Callable[[Any], Any] | None:
    """PR 2's ``authorisation_matrix(PolicySet) -> Matrix``, or None before it
    lands. Returns roles (ordered, ``default`` first), tools (ordered), and
    ``cell(tool, role) -> MatrixCell`` with ``.mode`` and ``.constraint_text``."""
    try:
        from hexgate.security.matrix import authorisation_matrix
    except ImportError:
        return None
    return authorisation_matrix


PENDING_MATRIX_REASON = (
    "The authorisation-matrix helper is not present in this build "
    "(hexgate.security.matrix), so no matrix could be derived from the "
    "resolved bundle."
)
