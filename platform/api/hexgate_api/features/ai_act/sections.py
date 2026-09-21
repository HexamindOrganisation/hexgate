"""Pure builders for the annex's cover and five sections.

Every function here takes values the caller has already fetched and returns
plain JSON-able data. No I/O and no session, so the whole document shape is
testable without a database or a ClickHouse server — the service does the
reading, this module does the shaping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from hexgate.security.matrix import Matrix, MatrixCell
from hexgate.security.models import AGENT_RUN_TOOL, is_agent_reach_key

from hexgate_api.features.agents.service import missing_fields
from hexgate_api.features.ai_act import copy
from hexgate_api.models import Agent

# The mode strings PR 2's matrix cells carry, in the order a reader expects
# them; kept here so the annex is explicit about its own vocabulary rather
# than passing a bare value through.
MODE_LABELS = {"allow": "Allow", "approval": "Approval", "deny": "Deny"}

# Completeness is PR 1's rule, imported rather than restated: the same agent
# must not read as complete on the classification endpoint and incomplete in
# the signed report. The two disagreed while this slice had its own copy — for
# an agent with no entry at all, the copy here counted the Annex III reference
# as missing, which presumes a high-risk tier nobody has asserted yet.
#
# What stays local is presentation: the endpoint returns field names, and the
# document wants a phrase an auditor reads.
_FIELD_LABELS = {
    "intended_purpose": "intended purpose",
    "operator_role": "operator role",
    "risk_tier": "risk tier",
    "annex_iii_point": "Annex III reference",
    "oversight_owner_name": "human-oversight owner",
}


@dataclass(frozen=True)
class AgentRecord:
    """One agent's inputs to sections 1 and 2, gathered by the service.

    ``classification`` is PR 1's row or None (None = no entry recorded).
    ``matrix`` is the SDK's Matrix or None, in which case ``matrix_unavailable``
    says why — the two always arrive together.
    """

    agent: Agent
    version: int | None
    bundle_manifest: dict[str, Any] | None
    classification: Any | None
    recorded_by_email: str | None
    matrix: Matrix | None
    matrix_unavailable: str | None


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def missing_inventory_fields(classification: Any | None) -> list[str]:
    """The fields a classification entry lacks, as an auditor reads them.

    Delegates the rule to the agents slice, which owns it, and only maps the
    field names onto labels. An unknown field falls back to its raw name
    rather than being dropped: a field added to the rule and not to the label
    map must still show up as missing.
    """
    return [_FIELD_LABELS.get(field, field) for field in missing_fields(classification)]


def _bundle_hashes(manifest: dict[str, Any] | None) -> dict[str, str | None] | None:
    """The identity of the bundle the matrix was derived from.

    Both hashes, not one: ``wasm_hash`` identifies the artifact the SDK
    actually evaluates, ``source_hash`` the resolved policy text it was
    compiled from. A bundle stored before the WASM compile succeeded has the
    second but not the first.
    """
    if manifest is None:
        return None
    return {
        "source_hash": manifest.get("source_hash"),
        "wasm_hash": manifest.get("wasm_hash"),
    }


def build_inventory(records: list[AgentRecord]) -> dict[str, Any]:
    """Section 1 — one row per agent, incomplete rows included and labelled.

    An incomplete entry is never omitted: a reader has to be able to see that
    an agent exists and has not been classified, which is itself the finding.
    """
    rows = []
    for record in records:
        entry = record.classification
        missing = missing_inventory_fields(entry)
        rows.append(
            {
                "agent_name": record.agent.name,
                "agent_id": record.agent.id,
                "version": record.version,
                "bundle": _bundle_hashes(record.bundle_manifest),
                "intended_purpose": getattr(entry, "intended_purpose", None),
                "operator_role": getattr(entry, "operator_role", None),
                "risk_tier": getattr(entry, "risk_tier", None),
                "annex_iii_point": getattr(entry, "annex_iii_point", None),
                "oversight_owner": {
                    "name": getattr(entry, "oversight_owner_name", None),
                    "contact": getattr(entry, "oversight_owner_contact", None),
                },
                "recorded_by": {
                    "user_id": getattr(entry, "recorded_by_user_id", None),
                    "email": record.recorded_by_email,
                },
                "recorded_at": _iso(getattr(entry, "recorded_at", None)),
                "checker_last_update_date": _iso(
                    getattr(entry, "checker_last_update_date", None)
                ),
                "complete": not missing,
                "missing_fields": missing,
            }
        )
    return {
        "articles": ["Art. 6", "Annex III", "Art. 26(2)"],
        "attribution": copy.INVENTORY_ATTRIBUTION,
        "agents": rows,
    }


def _cell(cell: MatrixCell) -> dict[str, Any]:
    """One ``MatrixCell`` as annex JSON, with the heading an auditor reads."""
    return {
        "mode": cell.mode,
        "label": MODE_LABELS.get(cell.mode, cell.mode),
        "constraint": cell.constraint_text,
    }


def _matrix_rows(matrix: Matrix) -> list[dict[str, Any]]:
    """One row per tool, one cell per role. ``Matrix.cells`` is complete, so
    every pair resolves; ``Matrix.cell`` raising off-grid is deliberate there
    and would be a bug here rather than a deny."""
    return [
        {
            "tool": tool,
            "cells": {role: _cell(matrix.cell(tool, role)) for role in matrix.roles},
        }
        for tool in matrix.tools
    ]


def _matrix_json(matrix: Matrix) -> dict[str, Any]:
    """The grid, its fallback row, and the aliased-default caveat.

    ``any_other_tool`` is ``Matrix.defaults`` — each role's standing answer for
    an ordinary tool the grid does not list. It has to be stated rather than
    assumed: a role that sets a permissive ``default_policy`` really does allow
    the tools it never lists, so a document asserting "anything unlisted is
    denied" would be evidencing a control nobody enforces. Unlisted ``agent.*``
    keys are closed-world and deny whatever the default says, which is why the
    row is labelled for ordinary tools only.

    ``aliased_default_role`` is set when the policy declared no ``default`` and
    the loader promoted a named role into that slot. The ``default`` column is
    then a duplicate of that role rather than a baseline anyone authored, and
    the report says so instead of presenting it as one.
    """
    return {
        "roles": list(matrix.roles),
        "tools": list(matrix.tools),
        "rows": _matrix_rows(matrix),
        "any_other_tool": {
            "note": copy.ANY_OTHER_TOOL_NOTE,
            "cells": {role: _cell(cell) for role, cell in matrix.defaults.items()},
        },
        # Derived, not asserted: whether either agent-level gate fires is an
        # opt-in signal from the policy, so a Deny on an agent.* row is only a
        # control when the policy declared such rules in the first place.
        #
        # Keyed on the reach prefixes and on agent.run SEPARATELY, not on
        # ``is_agent_key`` — that is true for either, so a policy declaring
        # only admission would have been reported as carrying reach rules it
        # does not have (``declares_reach`` keys on the prefixes alone).
        "agent_reach": (
            copy.AGENT_REACH_DECLARED
            if any(is_agent_reach_key(tool) for tool in matrix.tools)
            else copy.AGENT_REACH_NOT_DECLARED
        ),
        "agent_admission": (
            copy.AGENT_ADMISSION_DECLARED
            if AGENT_RUN_TOOL in matrix.tools
            else copy.AGENT_ADMISSION_NOT_DECLARED
        ),
        "aliased_default_role": matrix.aliased_default,
        "aliased_default_note": (
            copy.ALIASED_DEFAULT_NOTE.format(role=matrix.aliased_default)
            if matrix.aliased_default
            else None
        ),
    }


def build_controls(records: list[AgentRecord]) -> dict[str, Any]:
    """Section 2 — per-agent authorisation matrix, oversight, Art. 15 table.

    ``inventory_entry_complete`` travels with each agent so a renderer can
    apply the spec's rule (an incomplete agent's matrix is omitted from the
    PDF) without re-deriving completeness. The matrix stays in the annex
    either way — the annex is the signed record, not the presentation.
    """
    agents = []
    for record in records:
        entry = record.classification
        agents.append(
            {
                "agent_name": record.agent.name,
                "bundle": _bundle_hashes(record.bundle_manifest),
                "inventory_entry_complete": not missing_inventory_fields(entry),
                "authorisation_matrix": (
                    _matrix_json(record.matrix) if record.matrix is not None else None
                ),
                "matrix_unavailable_reason": record.matrix_unavailable,
                "human_oversight": copy.HUMAN_OVERSIGHT,
                "oversight_owner": {
                    "note": copy.OVERSIGHT_OWNER_NOTE,
                    "name": getattr(entry, "oversight_owner_name", None),
                    "contact": getattr(entry, "oversight_owner_contact", None),
                },
            }
        )
    return {
        "articles": ["Art. 9", "Art. 14", "Art. 15", "Art. 26(1)", "Art. 26(2)"],
        "deny_by_default": copy.DENY_BY_DEFAULT,
        "agents": agents,
        "data_protection_controls": [
            dict(row) for row in copy.DATA_PROTECTION_CONTROLS
        ],
    }


def _call_counts(breakdown: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the call count from an LLM breakdown row.

    ``summarize_llm_invocations`` also returns token sums, and those are sums
    over ROWS: ``distinct_events=True`` deduplicates the count but cannot
    deduplicate a ``sum``, which needs one row per event and so a different
    query. Emitting them here would put a figure that double-counts a retried
    send next to a deduplicated one in the same table, under a coverage
    statement that says every count is over distinct event ids. The spec asks
    section 3 for model calls by agent and model, not for tokens, so the
    honest shape is to drop them.

    Re-sorted by ``calls`` as well: the upstream breakdown is ranked by
    ``total_tokens``, so keeping that order would rank the rows by the very
    figure this function removes — three large-context calls would sit above
    four hundred small ones with nothing left in the document to explain why.
    Every other section 3 breakdown is sorted by the counter it shows.
    """
    return sorted(
        ({"key": row["key"], "calls": row["calls"]} for row in breakdown),
        key=lambda row: row["calls"],
        reverse=True,
    )


def build_activity(
    *,
    decisions: dict[str, Any],
    guard_refusals: int,
    ban_enforcements: dict[str, Any],
    llm: dict[str, Any],
    llm_errors: int,
    decision_sample: list[dict[str, Any]],
    approval_sample: list[dict[str, Any]],
) -> dict[str, Any]:
    """Section 3 — headline counters plus the breakdowns and samples.

    Every counter here is over distinct event ids (see the caller), so a
    retried send that the store has not yet merged away is counted once.
    """
    totals = decisions["totals"]
    calls = llm["totals"]["calls"]
    return {
        "articles": ["Art. 12", "Art. 19", "Art. 26(6)", "Art. 72", "Art. 73"],
        "counters": {
            "decisions": totals["all"],
            "denials": totals["deny"],
            "approvals_required": totals["needs_approval"],
            "guard_refusals": guard_refusals,
            "ban_enforcements": ban_enforcements["total"],
            "model_calls": calls,
            # None, not 0.0, when nothing was called: a rate over no calls is
            # undefined, and 0% would read as "no failures observed".
            "model_call_error_rate": (llm_errors / calls) if calls else None,
            "distinct_models": len(llm["by_model"]),
        },
        "decisions_by_agent_and_outcome": decisions["by_agent"],
        "decision_sample": {
            "note": copy.DECISION_SAMPLE_NOTE,
            "rows": decision_sample,
        },
        "approval_required_sample": {
            "note": copy.APPROVAL_SAMPLE_NOTE,
            "rows": approval_sample,
        },
        "ban_enforcements": {
            "note": copy.BAN_ENFORCEMENT_NOTE,
            "total": ban_enforcements["total"],
            "returned": len(ban_enforcements["rows"]),
            "truncated": len(ban_enforcements["rows"]) < ban_enforcements["total"],
            "rows": ban_enforcements["rows"],
        },
        "model_calls_by_agent": _call_counts(llm["by_agent"]),
        "model_calls_by_model": _call_counts(llm["by_model"]),
    }


# --- Section 4 gaps ---------------------------------------------------------
#
# The list is generated, not prose: each gap carries the condition that put it
# in the report, and a gap whose condition does not hold is absent. Two of the
# four are structural in v0 (no event exists to record the thing at all), and
# they say so in their own condition text rather than being silently always-on.

_GAP_APPROVAL_OUTCOMES = {
    "id": "approval_outcomes_unrecorded",
    "title": "Approval outcomes are not recorded",
    "statement": (
        "Calls in this period required human approval, but Hexgate records "
        "only that approval was required. The approval handler is the "
        "operator's own code and emits no event, so this report cannot show "
        "who approved a call, when, or whether the call then ran."
    ),
}
_GAP_SDK_ONLY_SECRET_CONTROLS = {
    "id": "sdk_only_secret_controls",
    "title": "Redactor and watch hits do not leave the SDK",
    "statement": (
        "Of the three secret controls in section 2, only secret_guard "
        "refusals produce an event. A secret_redactor strip and a "
        "secret_watch flag stay inside the host process, so this report "
        "evidences neither, whether or not the operator registered them."
    ),
}
_GAP_NO_RUN_GROUPING = {
    "id": "decisions_not_grouped_by_run",
    "title": "Decisions are not grouped by run",
    "statement": (
        "Some decisions in this period carry no run identifier, so they "
        "cannot be attributed to the agent run that produced them. The "
        "counters and tables in section 3 count individual decisions, not "
        "runs."
    ),
}


def build_gaps(
    *,
    approvals_required: int,
    decisions_without_run: int,
    incomplete_agents: list[str],
) -> list[dict[str, Any]]:
    """The gaps whose condition holds, in the order the spec fixes."""
    gaps: list[dict[str, Any]] = []
    if approvals_required > 0:
        gaps.append(
            {
                **_GAP_APPROVAL_OUTCOMES,
                "condition": (
                    f"{approvals_required} decision(s) in the period required approval"
                ),
            }
        )
    gaps.append(
        {
            **_GAP_SDK_ONLY_SECRET_CONTROLS,
            "condition": (
                "no event table records a secret_redactor or secret_watch hit "
                "in this platform version"
            ),
        }
    )
    if decisions_without_run > 0:
        gaps.append(
            {
                **_GAP_NO_RUN_GROUPING,
                "condition": (
                    f"{decisions_without_run} decision(s) in the period carry "
                    "no run identifier"
                ),
            }
        )
    for name in incomplete_agents:
        gaps.append(
            {
                "id": f"incomplete_inventory_entry:{name}",
                "title": f"Inventory entry for {name} is incomplete",
                "statement": (
                    f"The inventory entry for agent {name} is missing fields "
                    "listed in section 1, so this report does not state that "
                    "agent's intended purpose, asserted risk tier or named "
                    "oversight owner."
                ),
                "condition": (
                    "the agent's classification entry is missing at least one "
                    "required field"
                ),
            }
        )
    return gaps


def build_coverage(
    *,
    records_covered: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Section 4 — what is covered, how to read it, and what is missing."""
    return {
        "articles": ["Art. 21", "Art. 26(12)", "Art. 99(5)"],
        "records_covered": records_covered,
        "caveats": list(copy.CAVEATS),
        "gaps": gaps,
        "not_evidenced": list(copy.NOT_EVIDENCED),
    }


def build_signature_section(*, kid: str, jwks_url: str) -> dict[str, Any]:
    """Section 5 — how to verify the annex, minus the values it cannot hold.

    The digest and the signature are deliberately absent: they are computed
    over these exact bytes, so carrying them inside would be circular. They
    travel with the API response and the stored row, and a renderer reads them
    from there.
    """
    return {
        "statement": copy.SIGNATURE_STATEMENT,
        "algorithm": "Ed25519",
        "digest_algorithm": "SHA-256",
        "kid": kid,
        "jwks_url": jwks_url,
        "verification": list(copy.VERIFICATION_RECIPE),
    }


def build_cover(
    *,
    report_id: str,
    organization: dict[str, Any],
    project: dict[str, Any],
    period_start: datetime,
    period_end: datetime,
    agent_counts: dict[str, int],
    requested_by: dict[str, Any],
    generated_at: datetime,
) -> dict[str, Any]:
    """The cover block: who, what, when, and what this document is not."""
    return {
        "title": copy.TITLE,
        "spec_version": copy.SPEC_VERSION,
        "report_id": report_id,
        "what_this_is": copy.WHAT_THIS_IS,
        "what_this_is_not": copy.WHAT_THIS_IS_NOT,
        "organization": organization,
        "project": project,
        "period": {"start": _iso(period_start), "end": _iso(period_end)},
        "agents": agent_counts,
        "requested_by": requested_by,
        "generated_at": _iso(generated_at),
    }


def canonical_bytes(annex: dict[str, Any]) -> bytes:
    """The one serialization of an annex. Sign these exact bytes.

    Deterministic and stable across re-reads: no whitespace to drift, keys in
    the order the builders inserted them (``sort_keys`` would reorder a
    document whose section order is part of its meaning), and non-ASCII kept
    as characters so an operator's own alphabet survives the round-trip.
    """
    return json.dumps(annex, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
