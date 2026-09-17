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
from dataclasses import dataclass, field
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

from hexgate_api.constants import DEFAULT_PROJECT_ID, ROLE_MEMBER
from hexgate_api.core import keystore as keystore_mod
from hexgate.security.matrix import MatrixCell
from hexgate_api.features.ai_act import copy, sections, service, upstream
from hexgate_api.features.ai_act.sections import AgentRecord
from hexgate_api.main import app
from hexgate_api.models import Agent, AiActReport, OrganizationMember, User
from hexgate_api.query_scope import RETENTION_WINDOW
from hexgate_api.seeds.defaults import ensure_default_project
from tests.features.ai_act import seeded_events as seed

GOLDEN_PATH = Path(__file__).with_name("golden_annex.json")

# ---------------------------------------------------------------------------
# Stand-ins for the two interfaces another PR owns (see ai_act/upstream.py)
# ---------------------------------------------------------------------------


@dataclass
class StubClassification:
    """PR 1's ``AgentClassification`` row, exactly the fields the spec fixes."""

    agent_id: str
    intended_purpose: str | None = None
    operator_role: str | None = None
    risk_tier: str | None = None
    annex_iii_point: str | None = None
    oversight_owner_name: str | None = None
    oversight_owner_contact: str | None = None
    checker_last_update_date: date | None = None
    recorded_by_user_id: str = "usr_1"
    recorded_at: datetime = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@dataclass
class StubMatrix:
    """PR 2's ``Matrix``: ordered roles and tools, plus ``cell(tool, role)``.

    ``cell`` returns the real :class:`hexgate.security.matrix.MatrixCell`, not a
    tuple — the stub exists to build arbitrary grids cheaply, not to invent a
    second shape for the thing under test. Cells are given as
    ``(mode, constraint_text)`` pairs purely for terse test data.
    """

    roles: tuple[str, ...]
    tools: tuple[str, ...]
    cells: dict[tuple[str, str], tuple[str, str | None]] = field(default_factory=dict)

    def cell(self, tool: str, role: str) -> MatrixCell:
        # Deny-by-default fill, matching what PR 2 specifies for a tool a role
        # does not hold.
        mode, constraint = self.cells.get((tool, role), ("deny", None))
        return MatrixCell(mode=mode, constraint_text=constraint)


def _complete_classification(agent_id: str) -> StubClassification:
    return StubClassification(
        agent_id=agent_id,
        intended_purpose="Answer customer support questions about billing.",
        operator_role="deployer",
        risk_tier="high_risk",
        annex_iii_point="5(b)",
        oversight_owner_name="Dana Okafor",
        oversight_owner_contact="dana@example.com",
        checker_last_update_date=date(2026, 7, 4),
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
    matrix = StubMatrix(
        roles=("default", "support", "admin"),
        tools=("read_file", "refund"),
        cells={
            ("read_file", "default"): ("deny", None),
            ("read_file", "support"): ("allow", 'path startswith "/tickets/"'),
            ("read_file", "admin"): ("allow", None),
            ("refund", "default"): ("deny", None),
            ("refund", "support"): ("approval", "amount <= 100"),
            ("refund", "admin"): ("allow", None),
        },
    )
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


def test_when_no_entry_recorded_then_every_field_is_missing() -> None:
    missing = sections.missing_inventory_fields(None)
    assert "intended purpose" in missing and "risk tier" in missing
    assert "Annex III reference" in missing


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


def _matrix_fn(_policy_set: Any) -> StubMatrix:
    return StubMatrix(roles=("default",), tools=("read_file",))


def test_matrix_is_derived_from_the_bundle_source_happy_path() -> None:
    policy = "version: 1\ndefault_policy:\n  mode: deny\n"
    manifest = {"source_hash": hashlib.sha256(policy.encode()).hexdigest()}
    matrix, reason = service._matrix_for(
        _agent("a", source_hash="x"),
        manifest=manifest,
        policy_text=policy,
        matrix_fn=_matrix_fn,
        compose_errors=(ValueError,),
    )
    assert reason is None
    assert matrix is not None and matrix.tools == ("read_file",)


def test_when_policy_changed_since_the_bundle_compiled_then_no_matrix() -> None:
    """Tabulating the edited document would describe rules nothing enforces."""
    matrix, reason = service._matrix_for(
        _agent("a", source_hash="x"),
        manifest={"source_hash": "a-different-hash"},
        policy_text="version: 1\n",
        matrix_fn=_matrix_fn,
        compose_errors=(ValueError,),
    )
    assert matrix is None
    assert reason == copy.MATRIX_SOURCE_DRIFTED


def test_when_no_bundle_is_stored_then_no_matrix() -> None:
    matrix, reason = service._matrix_for(
        _agent("a", source_hash="x", with_bundle=False),
        manifest=None,
        policy_text="version: 1\n",
        matrix_fn=_matrix_fn,
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


def test_when_the_matrix_helper_is_absent_then_the_reason_says_so() -> None:
    """PR 2's helper is resolved at call time; until it lands section 2 states
    that no matrix could be derived rather than deriving one of its own."""
    matrix, reason = service._matrix_for(
        _agent("a", source_hash="x"),
        manifest={"source_hash": "x"},
        policy_text="version: 1\n",
        matrix_fn=None,
        compose_errors=(ValueError,),
    )
    assert matrix is None
    assert reason == upstream.PENDING_MATRIX_REASON


def test_matrix_cells_carry_mode_label_and_constraint() -> None:
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
        "constraint": "amount <= 100",
    }


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
