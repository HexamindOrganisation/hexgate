"""Tests for the AI Act evidence report slice.

Three things the spec asks to be pinned: a golden annex over a seeded slice of
the event tables, the generated gap list, and tenant isolation. The golden
lives in ``golden_annex.json``; each fixed paragraph is collapsed to a
``<copy:NAME>`` marker before comparing, so the file pins the document's shape,
its figures and which paragraph lands in which slot without carrying hundreds
of words a reviewer would have to re-diff.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from clickhouse_connect.driver.exceptions import OperationalError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import yaml
from hexgate.security import load_policy_set_from_dict
from hexgate.security.matrix import Matrix, authorisation_matrix
from hexgate.security.models import AGENT_RUN_TOOL

from hexgate_api.constants import DEFAULT_PROJECT_ID, ROLE_ADMIN, ROLE_MEMBER
from hexgate_api.core import keystore as keystore_mod
from hexgate_api.features.ai_act import copy, sections, service
from hexgate_api.features.ai_act.service import list_reports
from hexgate_api.features.ai_act.sections import AgentRecord
from hexgate_api.main import app
from hexgate_api.models import (
    Agent,
    AgentClassification,
    AiActReport,
    OrganizationMember,
    User,
)
from hexgate_api.query_scope import RETENTION_WINDOW
from hexgate_api.seeds.defaults import ensure_default_project
from tests.features.ai_act import seeded_events as seed

GOLDEN_PATH = Path(__file__).with_name("golden_annex.json")

# ---------------------------------------------------------------------------
# Nothing here is stubbed any more: both upstreams (#235 classification, #238
# matrix) are on main, so the golden is built from the real model and the real
# tabulator. A local stand-in would only pin this slice against a shape of its
# own invention — which is how the old stub came to carry constraint strings
# the real grammar rejects.


GOLDEN_POLICY_YAML = """
version: 1
roles:
  default:
    default_policy: { mode: deny }
  support:
    default_policy: { mode: deny }
    constraints: ['ctx.tenant == "acme"']
    tools:
      read_file:
        mode: allow
        file_scope:
          allowed_paths: ["/tickets/**"]
          denied_paths: ["/tickets/legal/**"]
      refund: { mode: approval_required, constraints: ["args.amount <= 100"] }
  admin:
    default_policy: { mode: deny }
    tools:
      read_file: { mode: allow }
      refund: { mode: allow }
  # A deliberately permissive fallback. It is the case the "any other tool" row
  # exists for: this role reaches tools the bundle never names, so a document
  # asserting deny-by-default over the whole grid would be evidencing a control
  # nobody enforces.
  sandbox:
    default_policy: { mode: allow }
"""


def _golden_matrix() -> Matrix:
    """The real tabulator over a real resolved policy set."""
    return authorisation_matrix(
        load_policy_set_from_dict(yaml.safe_load(GOLDEN_POLICY_YAML))
    )


def _complete_classification(agent_id: str) -> AgentClassification:
    return AgentClassification(
        id="acl_golden",
        agent_id=agent_id,
        intended_purpose="Answer customer support questions about billing.",
        operator_role="deployer",
        risk_tier="high_risk",
        annex_iii_point="5(b)",
        oversight_owner_name="Dana Okafor",
        oversight_owner_contact="dana@example.com",
        checker_last_update_date=date(2026, 7, 4),
        recorded_by_user_id="usr_1",
        recorded_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
    )


def _agent(name: str, *, source_hash: str, with_bundle: bool = True) -> Agent:
    return Agent(
        id=f"agt_{name}",
        project_id="proj_1",
        name=name,
        agent_yaml="name: " + name,
        policy_yaml="version: 1\n",
        bundle_manifest=(
            json.dumps({"source_hash": source_hash, "wasm_hash": "w" * 8})
            if with_bundle
            else None
        ),
    )


def _records() -> list[AgentRecord]:
    """Two agents: one classified and tabulated, one neither.

    Both halves matter to the document — a complete row with a matrix, and an
    unclassified agent whose matrix could not be derived, which section 1 must
    still list and section 4 must raise as a gap.
    """
    matrix = _golden_matrix()
    support = _agent("support_bot", source_hash="s" * 8)
    triage = _agent("triage_bot", source_hash="t" * 8, with_bundle=False)
    return [
        AgentRecord(
            agent=support,
            version=4,
            bundle_manifest=json.loads(support.bundle_manifest or "{}"),
            classification=_complete_classification(support.id),
            recorded_by_email="dana@example.com",
            matrix=matrix,
            matrix_unavailable=None,
        ),
        AgentRecord(
            agent=triage,
            version=1,
            bundle_manifest=None,
            classification=None,
            recorded_by_email=None,
            matrix=None,
            matrix_unavailable=copy.MATRIX_UNAVAILABLE_NO_BUNDLE,
        ),
    ]


@dataclass
class _Org:
    id: str = "org_1"
    slug: str = "acme"
    name: str = "Acme SAS"


@dataclass
class _Project:
    id: str = "proj_1"
    name: str = "support"
    org_id: str = "org_1"


@dataclass
class _User:
    id: str = "usr_1"
    email: str = "dana@example.com"


def _build_annex() -> dict[str, Any]:
    """The annex the golden pins: seeded events, fixed records, fixed clock."""
    events = service._read_events(
        seed.FakeClickHouse(),
        project_id="proj_1",
        start=seed.PERIOD_START,
        end=seed.PERIOD_END,
    )
    return service.build_annex(
        report_id="rpt_000000000000",
        organization=_Org(),  # type: ignore[arg-type]
        project=_Project(),  # type: ignore[arg-type]
        requested_by=_User(),  # type: ignore[arg-type]
        generated_at=datetime(2026, 9, 17, 10, 30, tzinfo=timezone.utc),
        period_start=seed.PERIOD_START,
        period_end=seed.PERIOD_END,
        records=_records(),
        events=events,
        signing_kid="sha256:0123456789abcdef",
    )


# ---------------------------------------------------------------------------
# Golden annex
# ---------------------------------------------------------------------------

# Every module-level string constant in copy.py, longest first so a constant
# that contains another is collapsed before its substring.
_COPY_CONSTANTS = sorted(
    (
        (value, name)
        for name, value in vars(copy).items()
        if name.isupper() and isinstance(value, str)
    ),
    key=lambda pair: len(pair[0]),
    reverse=True,
)


def _collapse_copy(node: Any) -> Any:
    """Replace any fixed sentence with a ``<copy:NAME>`` marker.

    The golden then asserts that the right constant landed in the right slot
    without repeating hundreds of words of prose that a reviewer would have to
    re-diff on every wording change.
    """
    if isinstance(node, dict):
        return {k: _collapse_copy(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_collapse_copy(v) for v in node]
    if isinstance(node, str):
        for value, name in _COPY_CONSTANTS:
            if node == value:
                return f"<copy:{name}>"
    return node


def test_annex_golden() -> None:
    annex = _collapse_copy(_build_annex())
    expected = json.loads(GOLDEN_PATH.read_text())
    assert annex == expected


def test_annex_counters_match_the_seeded_events() -> None:
    """The figures the golden pins, asserted by name so a golden refresh that
    changed a number could not pass unnoticed."""
    counters = _build_annex()["activity"]["counters"]
    assert counters == {
        "decisions": seed.DECISIONS,
        "denials": seed.DENIALS,
        "approvals_required": seed.APPROVALS_REQUIRED,
        "guard_refusals": seed.GUARD_REFUSALS,
        "ban_enforcements": seed.BAN_ENFORCEMENTS,
        "model_calls": seed.MODEL_CALLS,
        "model_call_error_rate": seed.MODEL_CALL_ERRORS / seed.MODEL_CALLS,
        "distinct_models": 2,
    }


def test_token_sums_never_reach_the_annex() -> None:
    """distinct_events deduplicates ``calls`` but cannot deduplicate a ``sum``.
    Emitting the token columns would put a figure that double-counts a retried
    send beside a deduplicated one, under a caveat saying every count is over
    distinct event ids."""
    activity = _build_annex()["activity"]
    for breakdown in ("model_calls_by_agent", "model_calls_by_model"):
        for row in activity[breakdown]:
            assert set(row) == {"key", "calls"}, breakdown


def test_gaps_are_cited_by_title_not_by_position() -> None:
    """build_gaps emits gap 1 only when an approval was required, so a prose
    pointer to "gap 2" resolves to a different gap in the common case."""
    prose = [
        text
        for name, value in vars(copy).items()
        if name.isupper()
        for text in _emitted_strings(value)
    ]
    assert prose
    assert not [t for t in prose if re.search(r"\bgap\s+\d", t)]

    # And the titles the prose cites instead must be titles a gap really has,
    # or the pointer rots the same way an ordinal does.
    titles = {
        g["title"]
        for g in sections.build_gaps(
            approvals_required=1,
            decisions_without_run=1,
            incomplete_agents=["a"],
        )
    }
    cited = {m for t in prose for m in re.findall(r'gap "([^"]+)"', t)}
    assert cited
    assert cited <= titles


def test_when_no_model_calls_then_error_rate_is_null_not_zero() -> None:
    """0.0 would read as "no failures observed" over calls that never happened."""
    activity = sections.build_activity(
        decisions={
            "totals": {"all": 0, "allow": 0, "deny": 0, "needs_approval": 0},
            "by_agent": [],
        },
        guard_refusals=0,
        ban_enforcements={"total": 0, "rows": []},
        llm={"totals": {"calls": 0}, "by_model": [], "by_agent": [], "by_user": []},
        llm_errors=0,
        decision_sample=[],
        approval_sample=[],
    )
    assert activity["counters"]["model_call_error_rate"] is None


def test_retried_decision_appears_once_in_the_sample() -> None:
    """The seed serves one event_id twice; the counters count distinct ids, so
    a sample showing it twice would contradict them."""
    sample = _build_annex()["activity"]["decision_sample"]["rows"]
    assert [row["event_id"] for row in sample] == [str(seed.DECISION_EVENT_ID)]


def test_every_count_is_over_distinct_event_ids() -> None:
    """Art. 99(5): a double-counted retry is exactly the misleading figure."""
    client = seed.FakeClickHouse()
    service._read_events(
        client, project_id="proj_1", start=seed.PERIOD_START, end=seed.PERIOD_END
    )
    counting = [
        sql for sql in client.statements if "count(" in sql or "uniqExact" in sql
    ]
    assert counting, "no counting statement was issued"
    for sql in counting:
        # count() OVER () carries a sample page's match total, which the annex
        # does not report; every other count is distinct-qualified.
        stripped = sql.replace("count() OVER ()", "")
        assert "count()" not in stripped, sql


def test_every_read_carries_a_memory_and_time_ceiling() -> None:
    """Measured: one generation over a 3.2M-row slice peaked at 2.58 GiB, and
    five concurrent ones OOM-killed the ClickHouse container — the kernel got
    there before the server's own tracker, so nothing named the cause. With a
    ceiling the report 503s instead of the server dying."""
    client = seed.FakeClickHouse()
    service._read_events(
        client, project_id="proj_1", start=seed.PERIOD_START, end=seed.PERIOD_END
    )
    assert client.settings
    for applied in client.settings:
        for key, value in service.REPORT_QUERY_SETTINGS.items():
            assert applied[key] == value


def test_sample_reads_drop_the_count_they_never_use() -> None:
    """count() OVER () is evaluated before LIMIT, so it buffers the whole
    180-day slice — 2644 MB vs 158 MB measured at 3.2M rows — for a total the
    annex takes from its own distinct-count queries instead."""
    client = seed.FakeClickHouse()
    service._read_events(
        client, project_id="proj_1", start=seed.PERIOD_START, end=seed.PERIOD_END
    )
    paged = [sql for sql in client.statements if "ORDER BY occurred_at DESC" in sql]
    assert len(paged) == 3
    for sql in paged:
        assert "count() OVER ()" not in sql


def test_every_read_is_scoped_to_the_project_and_period() -> None:
    client = seed.FakeClickHouse()
    service._read_events(
        client, project_id="proj_1", start=seed.PERIOD_START, end=seed.PERIOD_END
    )
    assert client.statements
    for sql, params in zip(client.statements, client.parameters):
        assert "project_id = {pid:String}" in sql
        assert params["pid"] == "proj_1"
        assert params["start_date"] == seed.PERIOD_START
        assert params["end_date"] == seed.PERIOD_END


def test_naive_driver_timestamps_are_stamped_utc() -> None:
    """clickhouse_connect hands back naive datetimes for DateTime64(_, 'UTC').
    Serialized as-is the annex would carry a zoneless timestamp, and the
    signature would cover the ambiguity."""
    naive = datetime(2026, 5, 4, 7, 8, 9)
    coverage = service._coverage(3, naive, naive)
    assert coverage["earliest_received_at"] == "2026-05-04T07:08:09+00:00"

    row = service._stamp_row_timestamps(
        {"event_id": "x", "occurred_at": naive, "received_at": naive}
    )
    assert row["occurred_at"].tzinfo is timezone.utc
    assert row["received_at"].tzinfo is timezone.utc


def test_aware_driver_timestamps_are_left_alone() -> None:
    aware = datetime(2026, 5, 4, 7, 8, 9, tzinfo=timezone.utc)
    assert service._as_utc(aware) is aware


def test_empty_period_reports_no_coverage_dates() -> None:
    """min/max over zero rows come back as the epoch, not NULL — a table with
    no events would otherwise claim coverage from 1970."""
    empty = service._coverage(0, datetime(1970, 1, 1, tzinfo=timezone.utc), None)
    assert empty["event_count"] == 0
    assert empty["earliest_received_at"] is None
    assert empty["latest_received_at"] is None
    assert empty["retention_days"] == RETENTION_WINDOW.days


# ---------------------------------------------------------------------------
# Wording — a conformity claim is a correctness bug here
# ---------------------------------------------------------------------------

_CONFORMITY_CLAIMS = re.compile(
    r"\b(?:is|are|was|were|remains?|fully)\s+compliant\b"
    r"|\bcomplies\s+with\b"
    r"|\bconforms?\s+(?:to|with)\b"
    r"|\bconformity\b"
    r"|\bcertif(?:ied|icate|ication)\b"
    r"|\bapproved\s+by\s+(?:the\s+)?(?:commission|regulator)\b",
    re.IGNORECASE,
)


def test_annex_never_claims_conformity() -> None:
    text = sections.canonical_bytes(_build_annex()).decode("utf-8")
    assert not _CONFORMITY_CLAIMS.findall(text)


def _emitted_strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _emitted_strings(v)]
    if isinstance(node, (list, tuple)):
        return [s for v in node for s in _emitted_strings(v)]
    return []


def test_copy_module_never_claims_conformity() -> None:
    """Every constant in copy.py, not just the ones one annex happens to use —
    a claim in a slot no fixture reaches is still shipped prose. Docstrings and
    comments are excluded on purpose: they describe this rule, so scanning them
    would flag the rule's own statement."""
    emitted = [
        text
        for name, value in vars(copy).items()
        if name.isupper()
        for text in _emitted_strings(value)
    ]
    assert emitted
    offenders = [t for t in emitted if _CONFORMITY_CLAIMS.findall(t)]
    assert offenders == []


# ---------------------------------------------------------------------------
# Section 1 completeness
# ---------------------------------------------------------------------------


def test_missing_inventory_fields_happy_path() -> None:
    assert sections.missing_inventory_fields(_complete_classification("agt_1")) == []


def test_when_no_entry_recorded_then_every_required_field_is_missing() -> None:
    """Not the Annex III reference, though: requiring it would presume a
    high-risk tier nobody has asserted. This is the agents slice's rule, and
    the report must report the same count the classification endpoint does."""
    missing = sections.missing_inventory_fields(None)
    assert missing == [
        "intended purpose",
        "operator role",
        "risk tier",
        "human-oversight owner",
    ]


def test_every_field_the_rule_can_return_has_a_label() -> None:
    """The real guard on the cross-slice coupling. ``missing_inventory_fields``
    maps the agents slice's field names onto phrases and falls back to the raw
    name for an unknown one — deliberate, since silently dropping a missing
    field would be worse. But the fallback ships ``recorded_by_user_id`` into a
    signed document, so a field added to that rule without a label here has to
    fail the suite instead.

    The test this replaced compared the LENGTH of the mapped list against the
    length of the rule's list. That is a comprehension over the same list, so
    it was equal for every possible input and could never detect label drift.
    """
    from hexgate_api.features.agents.service import (
        _HIGH_RISK_FIELDS,
        _REQUIRED_FIELDS,
    )

    rule_fields = set(_REQUIRED_FIELDS) | set(_HIGH_RISK_FIELDS)
    assert rule_fields <= set(sections._FIELD_LABELS)
    # And no label is a raw identifier, which is what the fallback would emit.
    for label in sections._FIELD_LABELS.values():
        assert "_" not in label, label


def test_missing_fields_are_labelled_in_the_rule_order() -> None:
    entry = _complete_classification("agt_1")
    entry.intended_purpose = None
    entry.annex_iii_point = None
    assert sections.missing_inventory_fields(entry) == [
        "intended purpose",
        "Annex III reference",
    ]


def test_an_erased_recorder_leaves_the_assertion_standing_without_a_name() -> None:
    """``recorded_by_user_id`` is nullable (ON DELETE SET NULL), and the
    completeness rule does not look at it — so such a row is a dated
    assertion with no asserter, and still reads as complete. The section's
    prose has to admit that rather than promise a name (#246 covers making it
    read as needing re-confirmation)."""
    erased = _complete_classification("agt_erased")
    erased.recorded_by_user_id = None
    row = sections.build_inventory(
        [
            AgentRecord(
                agent=_agent("erased_bot", source_hash="e" * 8),
                version=1,
                bundle_manifest=None,
                classification=erased,
                recorded_by_email=None,
                matrix=None,
                matrix_unavailable=copy.MATRIX_UNAVAILABLE_NO_BUNDLE,
            )
        ]
    )
    assert row["agents"][0]["recorded_by"] == {"user_id": None, "email": None}
    assert row["agents"][0]["recorded_at"] is not None
    assert row["agents"][0]["complete"] is True
    assert "erased" in row["attribution"]


def test_when_high_risk_without_annex_point_then_incomplete() -> None:
    entry = _complete_classification("agt_1")
    entry.annex_iii_point = None
    assert sections.missing_inventory_fields(entry) == ["Annex III reference"]


def test_when_not_high_risk_then_annex_point_is_not_required() -> None:
    entry = _complete_classification("agt_1")
    entry.risk_tier = "not_high_risk"
    entry.annex_iii_point = None
    assert sections.missing_inventory_fields(entry) == []


def test_incomplete_agents_are_listed_not_omitted() -> None:
    inventory = _build_annex()["inventory"]
    names = [row["agent_name"] for row in inventory["agents"]]
    assert names == ["support_bot", "triage_bot"]
    assert inventory["agents"][1]["complete"] is False


def test_incomplete_agent_keeps_its_matrix_slot_in_the_annex() -> None:
    """The PDF omits it; the annex is the signed record and keeps everything."""
    controls = _build_annex()["controls"]["agents"]
    triage = next(a for a in controls if a["agent_name"] == "triage_bot")
    assert triage["inventory_entry_complete"] is False
    assert triage["authorisation_matrix"] is None
    assert triage["matrix_unavailable_reason"] == copy.MATRIX_UNAVAILABLE_NO_BUNDLE


# ---------------------------------------------------------------------------
# Section 2 matrix sourcing
# ---------------------------------------------------------------------------


def test_matrix_is_derived_from_the_bundle_source_happy_path() -> None:
    policy = "version: 1\ndefault_policy:\n  mode: deny\ntools:\n  read_file: { mode: allow }\n"
    manifest = {"source_hash": hashlib.sha256(policy.encode()).hexdigest()}
    matrix, reason = service._matrix_for(
        _agent("a", source_hash="x"),
        manifest=manifest,
        policy_text=policy,
        compose_errors=(ValueError,),
    )
    assert reason is None
    assert matrix is not None and matrix.tools == ("read_file",)
    assert matrix.cell("read_file", "default").mode == "allow"


def test_when_policy_changed_since_the_bundle_compiled_then_no_matrix() -> None:
    """Tabulating the edited document would describe rules nothing enforces."""
    matrix, reason = service._matrix_for(
        _agent("a", source_hash="x"),
        manifest={"source_hash": "a-different-hash"},
        policy_text="version: 1\n",
        compose_errors=(ValueError,),
    )
    assert matrix is None
    assert reason == copy.MATRIX_SOURCE_DRIFTED


def test_when_no_bundle_is_stored_then_no_matrix() -> None:
    matrix, reason = service._matrix_for(
        _agent("a", source_hash="x", with_bundle=False),
        manifest=None,
        policy_text="version: 1\n",
        compose_errors=(ValueError,),
    )
    assert matrix is None
    assert reason == copy.MATRIX_UNAVAILABLE_NO_BUNDLE


async def test_when_a_modular_project_does_not_resolve_then_no_drift_is_claimed(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unresolved case must not fall back to policy_yaml — for a modular
    agent that is the deny-all placeholder, whose hash misses the manifest's
    source_hash and so would read as "the operator edited their policy"."""
    from hexgate_api.features.policy_modules import service as modules

    class _Unresolvable(Exception):
        pass

    async def _is_modular(_session, _project_id) -> bool:
        return True

    async def _explode(_session, _project_id, _agents):
        raise _Unresolvable("a capability module was deleted")

    monkeypatch.setattr(modules, "is_modular", _is_modular)
    monkeypatch.setattr(modules, "resolved_yaml_by_agent", _explode)
    monkeypatch.setattr(modules, "compose_error_types", lambda: (_Unresolvable,))

    async with session_factory() as session:
        agent = _agent("drift_probe", source_hash="s" * 8)
        agent.project_id = DEFAULT_PROJECT_ID
        session.add(agent)
        await session.commit()
        records = await service._agent_records(session, DEFAULT_PROJECT_ID)

    probe = next(r for r in records if r.agent.name == "drift_probe")
    assert probe.matrix is None
    assert probe.matrix_unavailable == copy.MATRIX_PROJECT_UNRESOLVED
    # Every agent in the project is in the same state, and none of them is
    # told their policy drifted.
    reasons = {r.matrix_unavailable for r in records}
    assert reasons == {copy.MATRIX_PROJECT_UNRESOLVED}


def test_matrix_cells_carry_mode_label_and_constraint() -> None:
    """Straight from the real tabulator: the role-wide fence composed with the
    tool's own constraint, and a deny carrying no constraint at all."""
    controls = _build_annex()["controls"]["agents"]
    support = next(a for a in controls if a["agent_name"] == "support_bot")
    rows = {
        row["tool"]: row["cells"] for row in support["authorisation_matrix"]["rows"]
    }
    assert rows["refund"]["default"] == {
        "mode": "deny",
        "label": "Deny",
        "constraint": None,
    }
    assert rows["refund"]["support"] == {
        "mode": "approval",
        "label": "Approval",
        "constraint": '(ctx.tenant == "acme") and (args.amount <= 100)',
    }
    # file_scope is labelled prose, not an expression in the constraint
    # grammar — the annex carries it as the tabulator rendered it.
    assert (
        "file_scope: file_path must be present"
        in (rows["read_file"]["support"]["constraint"])
    )


def test_permissive_default_is_stated_not_assumed_away() -> None:
    """The reason the "any other tool" row exists: a role whose policy sets a
    permissive fallback really does reach tools the bundle never names, so the
    document must state that rather than assert deny over the whole grid."""
    controls = _build_annex()["controls"]["agents"]
    support = next(a for a in controls if a["agent_name"] == "support_bot")
    fallback = support["authorisation_matrix"]["any_other_tool"]["cells"]
    assert fallback["sandbox"]["mode"] == "allow"
    assert fallback["support"]["mode"] == "deny"
    assert fallback["default"]["mode"] == "deny"


# A blanket claim that unlisted tools are denied. The merged tabulator proves
# this false for a role with a permissive ``default_policy``, so section 2 must
# point at the fallback row instead of asserting over the grid.
#
# Matched as a family of phrasings, because the sentence this replaced was one
# 25-character literal from a draft that no longer exists — it could only have
# caught a re-introduction word for word. The first cut of this regex was no
# better: it required the copula and "denied" to be adjacent, so a single
# adverb ("is ALWAYS denied") walked straight through, including the exact
# sentence the previous round removed. Hence the adverb allowance, the
# denied/refused pair, and the absent/omits triggers — and hence
# ``_BLANKET_DENY_PHRASINGS`` below, which is the part that actually keeps this
# honest: a guard is only worth what its corpus covers.
_BLANKET_DENY_CLAIM = re.compile(
    # "<something the policy does not list> ... is [adverb] denied/refused"
    r"(?:\b(?:un(?:listed|named|mentioned))\b"
    r"|\b(?:does|do|did)\s+not\s+(?:name|list|mention)\b"
    r"|\bnot\s+(?:named|listed|mentioned)\b"
    r"|\bnever\s+(?:names?|lists?|mentions?)\b"
    r"|\babsent\s+from\b"
    r"|\bomits?\b"
    # "no role's policy lists", "no policy names" — the negation sits on the
    # subject rather than on the verb.
    r"|\bno\s+[^.]{0,30}?(?:lists?|names?|mentions?)\b)"
    r"[^.]{0,80}?\b(?:is|are|becomes?)\s+(?:\w+\s+){0,2}(?:denied|refused)\b",
    re.IGNORECASE,
)

# Every phrasing the guard must catch. Two of these are verbatim sentences the
# review rounds actually removed from copy.py; the rest are the near-misses
# that defeated the first regex.
_BLANKET_DENY_PHRASINGS = (
    "A tool that a role's resolved policy does not mention is denied for that role.",
    "A tool that a role's resolved policy does not mention is always denied for that role.",
    "an agent the policy never names is always denied",
    "Any tool not listed in the bundle is denied for every role.",
    "Anything the policy does not name is denied.",
    "An unlisted tool is denied.",
    "An unlisted tool is refused outright.",
    "Tools the policy does not mention are therefore denied.",
    "A tool absent from the resolved bundle is denied.",
    "Anything the bundle omits is denied.",
    "A tool the policy does not name is implicitly denied.",
    "A tool that no role's policy lists is denied.",
)


def test_section_two_never_asserts_deny_over_tools_the_bundle_does_not_name() -> None:
    """The claim the merged tabulator disproves. Asserted against the rendered
    section 2 of an annex whose ``sandbox`` role really does have a permissive
    fallback, so the prose and the data are checked against each other rather
    than the prose against itself."""
    controls = _build_annex()["controls"]
    support = next(a for a in controls["agents"] if a["agent_name"] == "support_bot")
    fallbacks = support["authorisation_matrix"]["any_other_tool"]["cells"]
    assert any(cell["mode"] != "deny" for cell in fallbacks.values()), (
        "the fixture must contain a permissive fallback for this test to mean anything"
    )
    rendered = json.dumps(controls, ensure_ascii=False)
    assert not _BLANKET_DENY_CLAIM.findall(rendered)
    # The pointer to the fallback row is the replacement for that claim.
    assert "any other tool" in copy.DENY_BY_DEFAULT


def test_the_blanket_deny_tripwire_actually_catches_the_claim() -> None:
    """A guard that matches nothing is worse than no guard: it makes the suite
    look stronger than it is. The first version of this test pinned only four
    phrasings it had been written against, and the regex passed all four while
    missing eight others — so the corpus is the test."""
    for claim in _BLANKET_DENY_PHRASINGS:
        assert _BLANKET_DENY_CLAIM.findall(claim), f"slipped through: {claim}"


def test_the_blanket_deny_tripwire_does_not_fire_on_the_shipped_prose() -> None:
    """The other half: a guard that matches everything gets deleted."""
    shipped = [
        text
        for name, value in vars(copy).items()
        if name.isupper()
        for text in _emitted_strings(value)
    ]
    assert shipped
    assert [t for t in shipped if _BLANKET_DENY_CLAIM.findall(t)] == []


def _matrix_from(policy_yaml: str) -> Matrix:
    return authorisation_matrix(load_policy_set_from_dict(yaml.safe_load(policy_yaml)))


_DENY_DEFAULT_ROLE = (
    "version: 1\nroles:\n  default:\n    default_policy: { mode: deny }\n"
)


def test_agent_reach_is_reported_only_when_the_policy_declares_it() -> None:
    """Whether either agent-level gate runs is an opt-in signal from the
    policy, so a Deny on an agent.* row is a control only if such rules exist.
    The golden policy declares neither."""
    rendered = sections._matrix_json(_golden_matrix())
    assert rendered["agent_reach"] == copy.AGENT_REACH_NOT_DECLARED
    assert rendered["agent_admission"] == copy.AGENT_ADMISSION_NOT_DECLARED

    with_reach = sections._matrix_json(
        _matrix_from(
            _DENY_DEFAULT_ROLE
            + "    agents:\n      billing: { mode: allow, via: [handoff] }\n"
        )
    )
    assert with_reach["agent_reach"] == copy.AGENT_REACH_DECLARED
    assert with_reach["agent_admission"] == copy.AGENT_ADMISSION_NOT_DECLARED


def test_admission_only_policy_is_not_reported_as_declaring_reach() -> None:
    """``is_agent_key`` is true for ``agent.run`` as well as the reach
    prefixes, so keying both statements off it claimed reach rules an
    admission-only policy does not have — and in the overstating direction."""
    admission_only = sections._matrix_json(
        _matrix_from(_DENY_DEFAULT_ROLE + "    admission: { mode: allow }\n")
    )
    assert (
        AGENT_RUN_TOOL
        in _matrix_from(_DENY_DEFAULT_ROLE + "    admission: { mode: allow }\n").tools
    )
    assert admission_only["agent_admission"] == copy.AGENT_ADMISSION_DECLARED
    assert admission_only["agent_reach"] == copy.AGENT_REACH_NOT_DECLARED


def test_aliased_default_is_flagged_when_the_policy_declares_none() -> None:
    """A promoted role in the default slot is not a baseline anyone authored,
    and presenting it as one would evidence a role the policy never declared."""
    aliased = authorisation_matrix(
        load_policy_set_from_dict(
            yaml.safe_load(
                "version: 1\nroles:\n  operator:\n    default_policy: { mode: deny }\n"
            )
        )
    )
    rendered = sections._matrix_json(aliased)
    assert rendered["aliased_default_role"] == "operator"
    assert "operator" in rendered["aliased_default_note"]


def test_no_aliased_default_note_when_the_policy_declares_one() -> None:
    rendered = sections._matrix_json(_golden_matrix())
    assert rendered["aliased_default_role"] is None
    assert rendered["aliased_default_note"] is None


# ---------------------------------------------------------------------------
# Section 4 gaps
# ---------------------------------------------------------------------------


def test_build_gaps_happy_path() -> None:
    """All four gaps, in the order the spec fixes them."""
    gaps = sections.build_gaps(
        approvals_required=2,
        decisions_without_run=5,
        incomplete_agents=["triage_bot"],
    )
    assert [g["id"] for g in gaps] == [
        "approval_outcomes_unrecorded",
        "sdk_only_secret_controls",
        "decisions_not_grouped_by_run",
        "incomplete_inventory_entry:triage_bot",
    ]
    assert all(g["condition"] for g in gaps)


def test_when_no_approval_was_required_then_that_gap_is_absent() -> None:
    gaps = sections.build_gaps(
        approvals_required=0, decisions_without_run=1, incomplete_agents=[]
    )
    assert "approval_outcomes_unrecorded" not in {g["id"] for g in gaps}


def test_when_every_decision_carries_a_run_then_that_gap_is_absent() -> None:
    gaps = sections.build_gaps(
        approvals_required=1, decisions_without_run=0, incomplete_agents=[]
    )
    assert "decisions_not_grouped_by_run" not in {g["id"] for g in gaps}


def test_one_gap_per_incomplete_agent() -> None:
    gaps = sections.build_gaps(
        approvals_required=0,
        decisions_without_run=0,
        incomplete_agents=["a", "b", "c"],
    )
    assert [g["id"] for g in gaps] == [
        "sdk_only_secret_controls",
        "incomplete_inventory_entry:a",
        "incomplete_inventory_entry:b",
        "incomplete_inventory_entry:c",
    ]


def test_sdk_only_secret_controls_gap_is_always_present_in_v0() -> None:
    """Its condition is structural: no table records a redactor or watch hit."""
    gaps = sections.build_gaps(
        approvals_required=0, decisions_without_run=0, incomplete_agents=[]
    )
    assert [g["id"] for g in gaps] == ["sdk_only_secret_controls"]


# ---------------------------------------------------------------------------
# Period
# ---------------------------------------------------------------------------


def test_period_defaults_to_the_full_retention_window() -> None:
    start, end = service.resolve_period(None, None)
    assert end - start == RETENTION_WINDOW
    assert abs((datetime.now(timezone.utc) - end).total_seconds()) < 5


def test_period_longer_than_retention_is_clamped_to_it() -> None:
    """A period claiming more coverage than the store can hold would overstate
    what the document evidences. Clamped to the retention floor, so the span
    lands a hair under the window rather than exactly on it."""
    requested_end = datetime.now(timezone.utc)
    start, end = service.resolve_period(
        requested_end - timedelta(days=400), requested_end
    )
    assert RETENTION_WINDOW - (end - start) < timedelta(seconds=5)
    assert end - start <= RETENTION_WINDOW


def test_period_entirely_older_than_retention_is_refused() -> None:
    """A retrospective quarter whose rows the TTL already deleted would
    otherwise produce a signed document reading zero decisions, zero denials
    and zero enforcements, with nothing in it to say the data had expired."""
    now = datetime.now(timezone.utc)
    with pytest.raises(service.InvalidPeriod) as exc:
        service.resolve_period(
            now - RETENTION_WINDOW - timedelta(days=260),
            now - RETENTION_WINDOW - timedelta(days=200),
        )
    assert "retention" in str(exc.value)


def test_partly_expired_period_is_reported_from_the_retention_floor() -> None:
    """Clamped, not refused — and the clamped start is what the annex records,
    so the document cannot claim coverage the store does not have."""
    now = datetime.now(timezone.utc)
    start, end = service.resolve_period(now - timedelta(days=300), now)
    assert start >= now - RETENTION_WINDOW - timedelta(seconds=5)
    assert end <= now + timedelta(seconds=5)


def test_expired_period_returns_400_rather_than_a_signed_empty_report(
    client: TestClient, session_factory
) -> None:
    now = datetime.now(timezone.utc)
    old_end = (now - RETENTION_WINDOW - timedelta(days=200)).isoformat()
    old_start = (now - RETENTION_WINDOW - timedelta(days=260)).isoformat()
    project_id = _make_project(client)
    r = client.post(
        f"/v1/projects/{project_id}/ai-act/report",
        json={"from": old_start, "to": old_end},
    )
    assert r.status_code == 400

    async def _count() -> int:
        async with session_factory() as s:
            return len((await s.exec(select(AiActReport))).all())

    assert asyncio.get_event_loop().run_until_complete(_count()) == 0


def test_model_call_breakdowns_are_ranked_by_the_count_they_show() -> None:
    """Upstream ranks by total_tokens, which the annex deliberately omits — so
    a few large-context calls would outrank many small ones with nothing in the
    document to explain the order."""
    ranked = sections._call_counts(
        [
            {"key": "research_bot", "calls": 3, "total_tokens": 360_000},
            {"key": "support_agent", "calls": 400, "total_tokens": 160_000},
        ]
    )
    assert [row["key"] for row in ranked] == ["support_agent", "research_bot"]
    assert [row["calls"] for row in ranked] == [400, 3]


def test_inverted_period_rejected() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(service.InvalidPeriod):
        service.resolve_period(now, now - timedelta(days=1))


def test_empty_period_rejected() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(service.InvalidPeriod):
        service.resolve_period(now, now)


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def test_canonical_bytes_are_stable_and_hold_non_ascii() -> None:
    annex = {"b": "café", "a": 1}
    first = sections.canonical_bytes(annex)
    assert sections.canonical_bytes(dict(annex)) == first
    assert "café" in first.decode("utf-8")
    # Insertion order, not sorted: section order is part of the document.
    assert first.startswith(b'{"b"')


# ---------------------------------------------------------------------------
# Endpoints — real auth + SQLite, seeded ClickHouse
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as bootstrap:
        await ensure_default_project(bootstrap)
    yield factory
    await engine.dispose()


@pytest.fixture
def fake_clickhouse() -> seed.FakeClickHouse:
    return seed.FakeClickHouse()


@pytest_asyncio.fixture
async def client(session_factory, fake_clickhouse, tmp_path) -> TestClient:
    from hexgate_api.core.db import get_session
    from hexgate_api.core.keystore import FileKeyStore
    from hexgate_api.deps.clickhouse import require_clickhouse

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[require_clickhouse] = lambda: fake_clickhouse
    original_keystore = keystore_mod.keystore
    keystore_mod.keystore = FileKeyStore(base_dir=tmp_path / "keystore")
    keystore_mod.keystore.ensure_keypair()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        keystore_mod.keystore = original_keystore


def _signup_and_login(client: TestClient, email: str, password: str) -> None:
    r = client.post("/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    r = client.post(
        "/v1/auth/cookie/login", data={"username": email, "password": password}
    )
    assert r.status_code == 204, r.text


def _make_project(
    client: TestClient,
    *,
    email: str = "owner@example.com",
    password: str = "correcthorsebattery",
    name: str = "proj",
) -> str:
    _signup_and_login(client, email, password)
    org_id = client.get("/v1/orgs").json()[0]["id"]
    r = client.post(f"/v1/orgs/{org_id}/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _add_agent(session_factory, *, project_id: str, name: str) -> None:
    async with session_factory() as s:
        s.add(
            Agent(
                id=f"agt_{uuid.uuid4().hex[:8]}",
                project_id=project_id,
                name=name,
                agent_yaml=f"name: {name}\n",
                policy_yaml="version: 1\ndefault_policy:\n  mode: deny\n",
            )
        )
        await s.commit()


def test_generate_report_happy_path(client: TestClient, session_factory) -> None:
    project_id = _make_project(client)
    asyncio.get_event_loop().run_until_complete(
        _add_agent(session_factory, project_id=project_id, name="support_bot")
    )
    r = client.post(f"/v1/projects/{project_id}/ai-act/report", json={})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"].startswith("rpt_")
    assert body["annex"]["cover"]["project"]["id"] == project_id
    assert body["annex"]["activity"]["counters"]["decisions"] == seed.DECISIONS
    # No classification entry exists, so the one agent reads as incomplete and
    # raises its own gap.
    assert body["annex"]["cover"]["agents"] == {
        "registered": 1,
        "complete": 0,
        "incomplete": 1,
    }
    assert "incomplete_inventory_entry:support_bot" in {
        g["id"] for g in body["annex"]["coverage"]["gaps"]
    }


def test_generated_signature_verifies_over_the_annex_digest(
    client: TestClient,
) -> None:
    """The recipe in section 5, executed: digest the bytes, verify the raw
    digest against the published key."""
    import base64

    project_id = _make_project(client)
    report_id = client.post(f"/v1/projects/{project_id}/ai-act/report", json={}).json()[
        "id"
    ]

    annex = client.get(f"/v1/projects/{project_id}/ai-act/reports/{report_id}/annex")
    assert annex.status_code == 200
    digest = hashlib.sha256(annex.content).digest()
    assert annex.headers["X-Hexgate-Annex-Sha256"] == digest.hex()

    jwks = client.get("/v1/.well-known/keys").json()["keys"][0]
    assert annex.headers["X-Hexgate-Signing-Kid"] == jwks["fingerprint"]
    public_bytes = base64.urlsafe_b64decode(jwks["x"] + "==")
    Ed25519PublicKey.from_public_bytes(public_bytes).verify(
        base64.b64decode(annex.headers["X-Hexgate-Signature"]), digest
    )


def test_annex_download_serves_the_exact_stored_bytes(
    client: TestClient, session_factory
) -> None:
    project_id = _make_project(client)
    report_id = client.post(f"/v1/projects/{project_id}/ai-act/report", json={}).json()[
        "id"
    ]
    r = client.get(f"/v1/projects/{project_id}/ai-act/reports/{report_id}/annex")

    async def _stored() -> str:
        async with session_factory() as s:
            row = (
                await s.exec(select(AiActReport).where(AiActReport.id == report_id))
            ).one()
            return row.annex_json

    assert r.content == asyncio.get_event_loop().run_until_complete(_stored()).encode(
        "utf-8"
    )
    assert report_id in r.headers["Content-Disposition"]


def test_history_lists_past_reports_newest_first(client: TestClient) -> None:
    project_id = _make_project(client)
    first = client.post(f"/v1/projects/{project_id}/ai-act/report", json={}).json()
    second = client.post(f"/v1/projects/{project_id}/ai-act/report", json={}).json()
    rows = client.get(f"/v1/projects/{project_id}/ai-act/reports").json()
    assert [row["id"] for row in rows][:2] == [second["id"], first["id"]]
    assert rows[0]["generated_by_email"] == "owner@example.com"
    assert rows[0]["annex_bytes"] > 0


def test_inverted_period_returns_400(client: TestClient) -> None:
    project_id = _make_project(client)
    r = client.post(
        f"/v1/projects/{project_id}/ai-act/report",
        json={"from": "2026-09-17T00:00:00Z", "to": "2026-03-21T00:00:00Z"},
    )
    assert r.status_code == 400


def test_requested_period_reaches_the_annex(client: TestClient) -> None:
    project_id = _make_project(client)
    body = client.post(
        f"/v1/projects/{project_id}/ai-act/report",
        json={"from": "2026-08-01T00:00:00Z", "to": "2026-09-01T00:00:00Z"},
    ).json()
    period = body["annex"]["cover"]["period"]
    assert period["start"].startswith("2026-08-01")
    assert period["end"].startswith("2026-09-01")


def test_clickhouse_unavailable_returns_503_and_stores_nothing(
    client: TestClient, fake_clickhouse, session_factory
) -> None:
    project_id = _make_project(client)

    def _boom(*_args, **_kwargs):
        raise OperationalError("clickhouse down")

    fake_clickhouse.query = _boom  # type: ignore[method-assign]
    r = client.post(f"/v1/projects/{project_id}/ai-act/report", json={})
    assert r.status_code == 503

    async def _count() -> int:
        async with session_factory() as s:
            return len((await s.exec(select(AiActReport))).all())

    assert asyncio.get_event_loop().run_until_complete(_count()) == 0


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def _add_outsider(session_factory, *, email: str) -> str:
    async with session_factory() as s:
        user = User(email=email)
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user.id


async def _add_org_member(
    session_factory, *, email: str, org_id: str, role: str
) -> str:
    """A user with a real membership in the project's org, at ``role``."""
    async with session_factory() as s:
        user = User(email=email)
        s.add(user)
        await s.commit()
        await s.refresh(user)
        s.add(
            OrganizationMember(
                id=str(uuid.uuid4()), user_id=user.id, org_id=org_id, role=role
            )
        )
        await s.commit()
        return user.id


def test_plain_org_member_cannot_read_the_report(
    client: TestClient, session_factory
) -> None:
    """The annex embeds ban-enforcement rows — who was blocked and the
    operator's free-text reason. Every other route serving that data is
    admin-gated (``/audit/ban-enforcements`` and all of ``/bans``), so a
    member reaching it through a report would route around that boundary
    rather than inherit it.

    This is the test whose absence let that through: every other auth case
    here used the org owner, who passes an admin check, so swapping the
    dependency changed no result.
    """
    project_id = _make_project(client)
    org_id = client.get("/v1/orgs").json()[0]["id"]
    member = asyncio.get_event_loop().run_until_complete(
        _add_org_member(
            session_factory, email="member@example.com", org_id=org_id, role=ROLE_MEMBER
        )
    )
    client.cookies.clear()
    headers = {"X-Dev-User": member}

    assert (
        client.post(
            f"/v1/projects/{project_id}/ai-act/report", json={}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/v1/projects/{project_id}/ai-act/reports", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/v1/projects/{project_id}/ai-act/reports/rpt_whatever/annex",
            headers=headers,
        ).status_code
        == 403
    )


def test_project_admin_can_read_the_report(client: TestClient, session_factory) -> None:
    """The other half: admin is enough, owner is not required."""
    project_id = _make_project(client)
    org_id = client.get("/v1/orgs").json()[0]["id"]
    admin = asyncio.get_event_loop().run_until_complete(
        _add_org_member(
            session_factory, email="admin@example.com", org_id=org_id, role=ROLE_ADMIN
        )
    )
    client.cookies.clear()
    r = client.post(
        f"/v1/projects/{project_id}/ai-act/report",
        json={},
        headers={"X-Dev-User": admin},
    )
    assert r.status_code == 201, r.text


def test_history_rows_never_carry_the_annex(
    client: TestClient, session_factory
) -> None:
    """``list_reports`` selects explicit columns, so the blob stays in Postgres.
    Loading it to measure a byte length pulled every annex a project had ever
    generated into the process; ``annex_bytes`` is a stored column now."""
    project_id = _make_project(client)
    body = client.post(f"/v1/projects/{project_id}/ai-act/report", json={}).json()

    async def _rows():
        async with session_factory() as s:
            return await list_reports(s, project_id)

    rows = asyncio.get_event_loop().run_until_complete(_rows())
    assert [type(r).__name__ for r in rows] == ["ReportSummary"]
    assert not hasattr(rows[0], "annex_json")
    # And the stored count is the real UTF-8 length of the signed bytes.
    assert rows[0].annex_bytes == body["annex_bytes"]
    annex = client.get(f"/v1/projects/{project_id}/ai-act/reports/{body['id']}/annex")
    assert rows[0].annex_bytes == len(annex.content)


def test_history_limit_and_offset_are_clamped(client: TestClient) -> None:
    """A negative LIMIT is an executor error on Postgres and silently ignored
    by SQLite, so an unclamped value would 500 only in production while the
    dev suite stayed green. Clamped like the audit reads."""
    project_id = _make_project(client)
    client.post(f"/v1/projects/{project_id}/ai-act/report", json={})
    for query in ("limit=-1", "limit=0", "offset=-5", "limit=99999999"):
        r = client.get(f"/v1/projects/{project_id}/ai-act/reports?{query}")
        assert r.status_code == 200, (query, r.text)
    assert (
        len(client.get(f"/v1/projects/{project_id}/ai-act/reports?limit=-1").json())
        == 1
    )


def test_summary_columns_and_fields_cannot_drift_apart() -> None:
    """``ReportSummary(*row)`` maps positionally, so reordering one list and
    not the other would silently move a value into the wrong field — a digest
    reported as a byte count, and nothing raising."""
    assert (
        tuple(c.key for c in service._SUMMARY_COLUMNS) == service.ReportSummary._fields
    )


def test_history_is_paginated(client: TestClient) -> None:
    project_id = _make_project(client)
    for _ in range(3):
        assert (
            client.post(f"/v1/projects/{project_id}/ai-act/report", json={}).status_code
            == 201
        )
    page = client.get(f"/v1/projects/{project_id}/ai-act/reports?limit=2").json()
    assert len(page) == 2
    rest = client.get(
        f"/v1/projects/{project_id}/ai-act/reports?limit=2&offset=2"
    ).json()
    assert len(rest) == 1
    assert {r["id"] for r in page}.isdisjoint({r["id"] for r in rest})


def test_outsider_cannot_generate_or_list_reports(
    client: TestClient, session_factory
) -> None:
    project_id = _make_project(client)
    outsider = asyncio.get_event_loop().run_until_complete(
        _add_outsider(session_factory, email="outsider@example.com")
    )
    client.cookies.clear()
    headers = {"X-Dev-User": outsider}
    assert (
        client.post(
            f"/v1/projects/{project_id}/ai-act/report", json={}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/v1/projects/{project_id}/ai-act/reports", headers=headers
        ).status_code
        == 403
    )


def test_report_from_another_project_reads_as_absent(
    client: TestClient, session_factory
) -> None:
    """404, not 403: a 403 would confirm the id exists somewhere else."""
    first = _make_project(client, name="first")
    report_id = client.post(f"/v1/projects/{first}/ai-act/report", json={}).json()["id"]
    org_id = client.get("/v1/orgs").json()[0]["id"]
    second = client.post(f"/v1/orgs/{org_id}/projects", json={"name": "second"}).json()[
        "id"
    ]
    r = client.get(f"/v1/projects/{second}/ai-act/reports/{report_id}/annex")
    assert r.status_code == 404


def test_another_orgs_member_is_refused(client: TestClient, session_factory) -> None:
    project_id = _make_project(client)
    other = asyncio.get_event_loop().run_until_complete(
        _add_outsider(session_factory, email="other-org@example.com")
    )

    async def _own_org() -> None:
        async with session_factory() as s:
            s.add(
                OrganizationMember(
                    id=str(uuid.uuid4()),
                    user_id=other,
                    org_id="00000000-0000-0000-0000-000000000001",
                    role=ROLE_MEMBER,
                )
            )
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_own_org())
    client.cookies.clear()
    r = client.get(
        f"/v1/projects/{project_id}/ai-act/reports", headers={"X-Dev-User": other}
    )
    assert r.status_code == 403
