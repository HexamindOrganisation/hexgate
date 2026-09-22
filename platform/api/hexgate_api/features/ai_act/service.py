"""Assemble, sign and store one AI Act evidence report.

Generation is synchronous: a 180-day window costs a handful of ClickHouse
aggregates plus one bounded sample per table. If that stops fitting in a
request it becomes a job; it is not one today.

Reads go through the existing feature services wherever one already answers
the question (``audit.summarize``, ``audit.list_decisions``,
``audit.list_ban_enforcements``, ``summarize_llm_invocations``). The two
queries written here answer questions none of those does: the conditional
distinct counts section 3 and section 4 need, and each table's coverage
metadata. Both build their WHERE through ``query_scope.scope_filters``, so
every read is project-scoped by the same code path as the dashboard's.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Iterable, NamedTuple

import yaml
from clickhouse_connect.driver.client import Client
from hexgate.security import load_policy_set_from_dict
from hexgate.security.matrix import Matrix, authorisation_matrix
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.clickhouse import ZERO_RUN_ID
from hexgate_api.core.ids import new_id
from hexgate_api.features.ai_act import copy, sections
from hexgate_api.features.ai_act.sections import AgentRecord
from hexgate_api.features.audit import service as audit
from hexgate_api.features.llm_invocations.service import (
    LLM_INVOCATION_TABLE,
    summarize_llm_invocations,
)
from hexgate_api.models import (
    Agent,
    AgentClassification,
    AgentVersion,
    AiActReport,
    Organization,
    Project,
    User,
    utcnow,
)
from hexgate_api.query_scope import RETENTION_WINDOW, prepare_date_range, scope_filters
from hexgate_api.schemas import AuditDecisionRow, BanEnforcementRow

logger = logging.getLogger(__name__)

# Where a verifier fetches the public half of the signing key. Stored as a
# path, not an absolute URL built from the request: the annex is signed, and
# baking a caller-controlled Host header into a signed document would let the
# request that generated it choose where a verifier looks for the key.
JWKS_PATH = "/v1/.well-known/keys"

# Bounded samples. Section 3 is evidence that a record exists and what it looks
# like, not a copy of the log — the log stays queryable through the audit API.
DECISION_SAMPLE_LIMIT = 20
APPROVAL_SAMPLE_LIMIT = 20
# Bans are the kill switch: the spec wants every enforcement, so the cap is
# high and the annex says when it clipped rather than pretending it did not.
BAN_ENFORCEMENT_LIMIT = 500

# The ``error_type`` a guard halt carries, set in the SDK's guard runner
# (``hexgate/guards/runner.py``) so a guard refusal stays distinguishable from
# a policy denial. Not an exported SDK constant; mirrored here deliberately.
GUARD_DENIED_ERROR_TYPE = "guard_denied"
# ``llm_invocation.status`` defaults to 'success'; anything else is a
# non-success outcome (the SDK sends "error").
LLM_SUCCESS_STATUS = "success"

# Per-query ceilings for every read this report issues.
#
# Measured, on a 3.2M-decision / 180-day slice: one generation peaked at
# 2.58 GiB in a single query, three concurrent generations took 10 s each, and
# five OOM-killed the ClickHouse container outright — the server's own
# max_server_memory_usage is 0.9x host RAM while the container has no memory
# limit, so the kernel wins the race and nothing lands in query_log naming the
# cause. An operator double-clicking Generate is enough to reach that.
#
# With a ceiling, the report is what fails, loudly and attributably: the driver
# raises a ClickHouseError, which the router already maps to 503 + Retry-After.
# The spill threshold keeps the counts EXACT while bounding what they cost —
# count(DISTINCT event_id) is uniqExact, whose per-group hash sets are
# aggregation state, so external GROUP BY moves them to disk rather than
# trading exactness for memory the way uniq() would.
REPORT_QUERY_SETTINGS = {
    "max_memory_usage": 1024 * 1024 * 1024,
    "max_bytes_before_external_group_by": 512 * 1024 * 1024,
    "max_execution_time": 30,
}


class _BoundedClient:
    """The shared ClickHouse client with :data:`REPORT_QUERY_SETTINGS` applied
    to every query, including the ones the audit and llm services issue on this
    slice's behalf.

    A wrapper rather than a ``settings=`` argument threaded through four shared
    read functions: the ceiling has to cover every read the report causes, and
    a keyword that each of those functions must remember to forward is a
    ceiling that a later read silently escapes.
    """

    def __init__(self, client: Client, settings: dict[str, Any]) -> None:
        self._client = client
        self._settings = settings

    def query(self, *args: Any, settings: dict[str, Any] | None = None, **kwargs: Any):
        return self._client.query(
            *args, settings={**self._settings, **(settings or {})}, **kwargs
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


COVERED_TABLES = (
    audit.DECISION_TABLE,
    audit.BAN_ENFORCEMENT_TABLE,
    LLM_INVOCATION_TABLE,
)


class InvalidPeriod(Exception):
    """The requested period is empty or inverted. Router -> 400."""


class ReportNotFound(Exception):
    """No report with this id in this project. Router -> 404."""


# ---------------------------------------------------------------------------
# Period
# ---------------------------------------------------------------------------


def resolve_period(
    period_start: datetime | None, period_end: datetime | None
) -> tuple[datetime, datetime]:
    """The period to report on, defaulting to the full retention window.

    Operator-supplied bounds go through ``prepare_date_range`` so they are
    clamped exactly as the dashboard's reads are: the end cannot sit in the
    future beyond the accepted clock skew, and the span cannot exceed
    retention.

    That is not sufficient on its own, because it clamps the span but not where
    the span SITS. A retrospective request — "January to February", the first
    thing anyone asks an evidence report for — is a legal 59-day span whose rows
    the TTL deleted months ago, and it would have produced a signed document
    reading zero decisions, zero denials and zero kill-switch enforcements for
    that quarter, with nothing in it to say the data had merely expired. So the
    period is also pinned to the retention floor: a partly-expired period is
    reported from the floor (and the coverage statement says why), and a period
    entirely below it is refused rather than signed.
    """
    now = datetime.now(timezone.utc)
    end = period_end or now
    start = period_start or end - RETENTION_WINDOW
    start, end = prepare_date_range(start, end)
    assert start is not None and end is not None  # both were non-None going in
    floor = now - RETENTION_WINDOW
    if end <= floor:
        raise InvalidPeriod(
            "the requested period is entirely older than the "
            f"{RETENTION_WINDOW.days}-day retention window, so no records "
            "survive in it to report on"
        )
    start = max(start, floor)
    if start >= end:
        raise InvalidPeriod("period start must be before period end")
    return start, end


def _since_hours(start: datetime) -> int:
    """The rolling-window fallback for ``scope_filters``.

    Only reached if the explicit range is rejected as invalid, which
    ``resolve_period`` has already ruled out — computed from the period anyway
    so the fallback would read the intended window rather than nothing.
    """
    elapsed = datetime.now(timezone.utc) - start
    return max(1, math.ceil(elapsed.total_seconds() / 3600))


# ---------------------------------------------------------------------------
# Registry side (SQL): agents, versions, classifications, matrices
# ---------------------------------------------------------------------------


async def _latest_versions(
    session: AsyncSession, agent_ids: Iterable[str]
) -> dict[str, int]:
    """Highest ``version`` per agent id, for the inventory's version column."""
    ids = list(agent_ids)
    if not ids:
        return {}
    rows = (
        await session.exec(
            select(AgentVersion.agent_id, AgentVersion.version).where(  # type: ignore[call-overload]
                AgentVersion.agent_id.in_(ids)  # type: ignore[attr-defined]
            )
        )
    ).all()
    latest: dict[str, int] = {}
    for agent_id, version in rows:
        if version > latest.get(agent_id, -1):
            latest[agent_id] = version
    return latest


async def _classifications(
    session: AsyncSession, agent_ids: Iterable[str]
) -> dict[str, AgentClassification]:
    """The operators' classification entries, keyed by agent id.

    An agent with no row is simply absent here, and reads as incomplete.
    """
    ids = list(agent_ids)
    if not ids:
        return {}
    rows = (
        await session.exec(
            select(AgentClassification).where(
                AgentClassification.agent_id.in_(ids)  # type: ignore[attr-defined]
            )
        )
    ).all()
    return {row.agent_id: row for row in rows}


async def emails_for_user_ids(
    session: AsyncSession, user_ids: set[str]
) -> dict[str, str]:
    """Email per user id, for attributing an entry or a report to a person.

    Ids with no live ``User`` row are omitted — an account deleted after it
    recorded a classification leaves the id in the annex with no email, rather
    than dropping the provenance the entry exists to carry.
    """
    wanted = {uid for uid in user_ids if uid}
    if not wanted:
        return {}
    rows = (
        await session.exec(
            select(User.id, User.email).where(User.id.in_(wanted))  # type: ignore[call-overload,attr-defined]
        )
    ).all()
    return {user_id: email for user_id, email in rows}


def _bundle_manifest(agent: Agent) -> dict[str, Any] | None:
    """The stored bundle manifest, parsed. None when there is no bundle."""
    if not agent.bundle_manifest:
        return None
    try:
        parsed = json.loads(agent.bundle_manifest)
    except ValueError:
        logger.warning("agent %s has an unparseable bundle manifest", agent.id)
        return None
    return parsed if isinstance(parsed, dict) else None


def _matrix_for(
    agent: Agent,
    *,
    manifest: dict[str, Any] | None,
    policy_text: str | None,
    compose_errors: tuple[type[BaseException], ...],
) -> tuple[Matrix | None, str | None]:
    """Tabulate one agent's compiled authority, or say why we cannot.

    The matrix must describe what the SDK enforces, which is the compiled
    bundle. The bundle's WASM is opaque, so the tabulation runs over the policy
    text the bundle was compiled *from* — and only after checking that text
    still hashes to the manifest's ``source_hash``. Without that check an
    operator who edited a policy since the last successful compile would get a
    control statement describing rules nothing is applying.

    A ``None`` matrix now only ever means the matrix is genuinely underivable —
    no stored bundle, a policy edited since it compiled, or a source that no
    longer parses. It is never "the helper is missing": ``authorisation_matrix``
    is imported at module scope.
    """
    if manifest is None or policy_text is None:
        return None, copy.MATRIX_UNAVAILABLE_NO_BUNDLE
    source_hash = hashlib.sha256(policy_text.encode("utf-8")).hexdigest()
    if source_hash != manifest.get("source_hash"):
        return None, copy.MATRIX_SOURCE_DRIFTED
    try:
        policy_set = load_policy_set_from_dict(yaml.safe_load(policy_text) or {})
        return authorisation_matrix(policy_set), None
    except compose_errors as exc:
        logger.warning("agent %s bundle source did not re-parse: %s", agent.id, exc)
        return None, copy.MATRIX_SOURCE_UNREADABLE


async def _agent_records(session: AsyncSession, project_id: str) -> list[AgentRecord]:
    """Gather sections 1 and 2's per-agent inputs in one pass over the registry."""
    from hexgate_api.features.policy_modules import service as modules

    agents = (
        await session.exec(
            select(Agent).where(Agent.project_id == project_id).order_by(Agent.name)  # type: ignore[arg-type]
        )
    ).all()
    versions = await _latest_versions(session, (a.id for a in agents))
    classifications = await _classifications(session, (a.id for a in agents))
    emails = await emails_for_user_ids(
        session,
        {
            getattr(entry, "recorded_by_user_id", "") or ""
            for entry in classifications.values()
        },
    )

    # A modular project's enforced policy is the resolved (role, agent) matrix,
    # not each agent's stored policy_yaml — for a modular agent that column is
    # the deny-all fallback (agents/compiler.DENY_ALL_POLICY_YAML). One resolve
    # for the whole project, like the compile path does.
    #
    # ``unresolved`` is not the same state as "no bundle": when the module set
    # does not compose there is nothing to tabulate, and falling back to
    # policy_yaml would hash the deny-all placeholder, miss the manifest's
    # source_hash, and make every agent report that the OPERATOR edited their
    # policy — a project-wide finding that did not happen, with a remediation
    # (save the policy again) that would not fix it.
    modular = bool(agents) and await modules.is_modular(session, project_id)
    resolved: dict[str, str] = {}
    unresolved = False
    if modular:
        try:
            # ..._auto, not the tier-store resolver: ``is_modular`` is true for
            # either store, and a compose-entry-file project resolved through
            # the tier store yields a deny-all set that hashes to nothing the
            # manifest knows — which _matrix_for would then report as the
            # operator having edited their policy. Same dispatcher the compile
            # path uses (agents/service.py), so the text we hash is the text
            # the stored source_hash was taken over.
            resolved = await modules.resolved_yaml_by_agent_auto(
                session, project_id, {a.name for a in agents}
            )
        except modules.compose_error_types() as exc:
            unresolved = True
            logger.warning(
                "project %s does not resolve; no matrices in this report: %s",
                project_id,
                exc,
            )

    compose_errors = modules.compose_error_types()
    records = []
    for agent in agents:
        manifest = _bundle_manifest(agent)
        if not modular:
            policy_text = agent.policy_yaml
        else:
            # A name absent from a map that did resolve is the same unusable
            # state as a project that did not resolve at all.
            policy_text = resolved.get(agent.name)
        if unresolved or (modular and policy_text is None):
            matrix, reason = None, copy.MATRIX_PROJECT_UNRESOLVED
        else:
            matrix, reason = _matrix_for(
                agent,
                manifest=manifest,
                policy_text=policy_text,
                compose_errors=compose_errors,
            )
        entry = classifications.get(agent.id)
        records.append(
            AgentRecord(
                agent=agent,
                version=versions.get(agent.id),
                bundle_manifest=manifest,
                classification=entry,
                recorded_by_email=emails.get(
                    getattr(entry, "recorded_by_user_id", "") or ""
                ),
                matrix=matrix,
                matrix_unavailable=reason,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Event side (ClickHouse)
# ---------------------------------------------------------------------------

# Conditional distinct counts. ClickHouse implements ``count(DISTINCT x)`` as
# ``uniqExact(x)`` and has no conditional form of the DISTINCT spelling, so the
# per-condition counters use ``uniqExactIf`` — the same aggregate, filtered.
_DECISION_AGGREGATES = (
    "count(DISTINCT event_id) AS events, "
    "uniqExactIf(event_id, error_type = {guard_error:String}) AS guard_refusals, "
    "uniqExactIf(event_id, run_id = {zero_run:UUID}) AS without_run, "
    "min(received_at) AS earliest, max(received_at) AS latest"
)
_LLM_AGGREGATES = (
    "count(DISTINCT event_id) AS events, "
    "uniqExactIf(event_id, status != {ok_status:String}) AS errors, "
    "min(received_at) AS earliest, max(received_at) AS latest"
)
_PLAIN_AGGREGATES = (
    "count(DISTINCT event_id) AS events, "
    "min(received_at) AS earliest, max(received_at) AS latest"
)


def _as_utc(value: datetime) -> datetime:
    """Stamp a driver datetime as UTC.

    ``DateTime64(3, 'UTC')`` comes back from clickhouse_connect as a NAIVE
    datetime whose value is already UTC. Serialized as-is it would put a
    timestamp with no zone into a signed evidence document, which an auditor
    cannot read unambiguously — and which nothing downstream could correct,
    because the signature covers it.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _coverage(events: int, earliest: Any, latest: Any) -> dict[str, Any]:
    """Coverage metadata for one table, with empty slices reported as empty.

    ``min``/``max`` over zero rows return the DateTime epoch rather than NULL,
    so a table with no events in the period would otherwise claim coverage
    starting in 1970.
    """
    return {
        "event_count": events,
        "earliest_received_at": _as_utc(earliest).isoformat() if events else None,
        "latest_received_at": _as_utc(latest).isoformat() if events else None,
        "retention_days": RETENTION_WINDOW.days,
    }


_ROW_TIMESTAMPS = ("occurred_at", "received_at")


def _stamp_row_timestamps(row: dict[str, Any]) -> dict[str, Any]:
    """Make a sampled row's timestamps explicitly UTC. See :func:`_as_utc`."""
    stamped = dict(row)
    for column in _ROW_TIMESTAMPS:
        value = stamped.get(column)
        if isinstance(value, datetime):
            stamped[column] = _as_utc(value)
    return stamped


def _dedup_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeated ``event_id``s, keeping the first occurrence.

    A retried send leaves two rows with one event_id until a background merge
    collapses them, and a list read is not ``FINAL``. The counters already
    count distinct ids; the sampled rows have to agree with them.
    """
    seen: set[Any] = set()
    unique = []
    for row in rows:
        key = str(row.get("event_id"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _read_events(
    client: Client,
    *,
    project_id: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Every ClickHouse read the report needs, in one synchronous batch.

    Called via ``asyncio.to_thread``: the clickhouse_connect client is sync, so
    a 180-day aggregation would otherwise stall every other in-flight request.

    Every read here narrows to the same slice, but not by sharing one WHERE:
    the delegated services build their own through the same ``scope_filters``.
    What makes them agree is that ``start``/``end`` are an explicit range, so
    each call binds the same two instants. Passing only ``since_hours`` would
    not — that branch anchors on ``now()`` per call, and an event arriving
    mid-assembly would land in some of the figures and not others.
    """
    client = _BoundedClient(client, REPORT_QUERY_SETTINGS)
    since_hours = _since_hours(start)
    where, params = scope_filters(
        project_id, since_hours, start_date=start, end_date=end
    )
    where_sql = " AND ".join(where)
    scope = {"project_id": project_id, "since_hours": since_hours}
    window = {"start_date": start, "end_date": end}

    decision_row = client.query(
        f"SELECT {_DECISION_AGGREGATES} FROM {audit.DECISION_TABLE} WHERE {where_sql}",
        parameters={
            **params,
            "guard_error": GUARD_DENIED_ERROR_TYPE,
            "zero_run": str(ZERO_RUN_ID),
        },
    ).result_rows[0]
    ban_row = client.query(
        f"SELECT {_PLAIN_AGGREGATES} FROM {audit.BAN_ENFORCEMENT_TABLE} "
        f"WHERE {where_sql}",
        parameters=params,
    ).result_rows[0]
    llm_row = client.query(
        f"SELECT {_LLM_AGGREGATES} FROM {LLM_INVOCATION_TABLE} WHERE {where_sql}",
        parameters={**params, "ok_status": LLM_SUCCESS_STATUS},
    ).result_rows[0]

    decisions = audit.summarize(client, **scope, **window, distinct_events=True)
    llm = summarize_llm_invocations(client, **scope, **window, distinct_events=True)
    # with_total=False on all three: the annex reports its own distinct counts
    # from the aggregate queries above, so each page's count() OVER () would
    # buffer the whole 180-day slice to produce a number nothing reads.
    decision_sample = audit.list_decisions(
        client, **scope, **window, limit=DECISION_SAMPLE_LIMIT, with_total=False
    )
    approval_sample = audit.list_decisions(
        client,
        **scope,
        **window,
        outcome="needs_approval",
        limit=APPROVAL_SAMPLE_LIMIT,
        with_total=False,
    )
    bans = audit.list_ban_enforcements(
        client, **scope, **window, limit=BAN_ENFORCEMENT_LIMIT, with_total=False
    )

    events, guard_refusals, without_run, dec_earliest, dec_latest = decision_row
    llm_events, llm_errors, llm_earliest, llm_latest = llm_row
    ban_events, ban_earliest, ban_latest = ban_row
    return {
        "decisions": decisions,
        "llm": llm,
        "guard_refusals": int(guard_refusals),
        "decisions_without_run": int(without_run),
        "llm_errors": int(llm_errors),
        "decision_sample": _dedup_rows(decision_sample["rows"]),
        "approval_sample": _dedup_rows(approval_sample["rows"]),
        "ban_rows": _dedup_rows(bans["rows"]),
        "ban_total": int(ban_events),
        "coverage": {
            audit.DECISION_TABLE: _coverage(int(events), dec_earliest, dec_latest),
            audit.BAN_ENFORCEMENT_TABLE: _coverage(
                int(ban_events), ban_earliest, ban_latest
            ),
            LLM_INVOCATION_TABLE: _coverage(int(llm_events), llm_earliest, llm_latest),
        },
    }


def _decision_rows_json(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Serialize sampled decisions through the audit wire model.

    Reusing ``AuditDecisionRow`` rather than dumping raw driver values keeps
    UUIDs and datetimes JSON-able (the annex is signed bytes) and keeps the
    sample's field names identical to what the audit API already serves.
    """
    return [
        AuditDecisionRow.model_validate(_stamp_row_timestamps(row)).model_dump(
            mode="json"
        )
        for row in rows
    ]


def _ban_rows_json(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        BanEnforcementRow.model_validate(_stamp_row_timestamps(row)).model_dump(
            mode="json"
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_annex(
    *,
    report_id: str,
    organization: Organization,
    project: Project,
    requested_by: User,
    generated_at: datetime,
    period_start: datetime,
    period_end: datetime,
    records: list[AgentRecord],
    events: dict[str, Any],
    signing_kid: str,
) -> dict[str, Any]:
    """The whole annex, from registry records and event reads. Pure."""
    inventory = sections.build_inventory(records)
    incomplete = [
        row["agent_name"] for row in inventory["agents"] if not row["complete"]
    ]
    activity = sections.build_activity(
        decisions=events["decisions"],
        guard_refusals=events["guard_refusals"],
        ban_enforcements={
            "total": events["ban_total"],
            "rows": _ban_rows_json(events["ban_rows"]),
        },
        llm=events["llm"],
        llm_errors=events["llm_errors"],
        decision_sample=_decision_rows_json(events["decision_sample"]),
        approval_sample=_decision_rows_json(events["approval_sample"]),
    )
    records_covered = [
        {
            "table": table,
            "contents": copy.TABLE_CONTENTS[table],
            **events["coverage"][table],
        }
        for table in COVERED_TABLES
    ]
    gaps = sections.build_gaps(
        approvals_required=activity["counters"]["approvals_required"],
        decisions_without_run=events["decisions_without_run"],
        incomplete_agents=incomplete,
    )
    return {
        "cover": sections.build_cover(
            report_id=report_id,
            organization={
                "id": organization.id,
                "slug": organization.slug,
                "name": organization.name,
            },
            project={"id": project.id, "name": project.name},
            period_start=period_start,
            period_end=period_end,
            agent_counts={
                "registered": len(records),
                "complete": len(records) - len(incomplete),
                "incomplete": len(incomplete),
            },
            requested_by={"user_id": requested_by.id, "email": requested_by.email},
            generated_at=generated_at,
        ),
        "inventory": inventory,
        "controls": sections.build_controls(records),
        "activity": activity,
        "coverage": sections.build_coverage(records_covered=records_covered, gaps=gaps),
        "signature": sections.build_signature_section(
            kid=signing_kid, jwks_url=JWKS_PATH
        ),
    }


async def generate_report(
    session: AsyncSession,
    clickhouse_client: Client,
    *,
    project: Project,
    requested_by: User,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> AiActReport:
    """Assemble, sign and store one report; returns the persisted row.

    The digest and signature cover the annex's exact bytes, and both are stored
    with them — so a later read serves the document that was signed rather than
    re-assembling it, which would pick up whatever arrived since.
    """
    from hexgate_api.core.keystore import keystore

    start, end = resolve_period(period_start, period_end)
    organization = await session.get(Organization, project.org_id)
    if organization is None:  # FK-backed; a missing org means a broken row
        raise ReportNotFound("project has no organization")

    records = await _agent_records(session, project.id)
    events = await asyncio.to_thread(
        _read_events,
        clickhouse_client,
        project_id=project.id,
        start=start,
        end=end,
    )

    report_id = new_id(AiActReport)
    generated_at = utcnow()
    annex = build_annex(
        report_id=report_id,
        organization=organization,
        project=project,
        requested_by=requested_by,
        generated_at=generated_at,
        period_start=start,
        period_end=end,
        records=records,
        events=events,
        signing_kid=keystore.fingerprint(),
    )
    annex_bytes = sections.canonical_bytes(annex)
    digest = hashlib.sha256(annex_bytes).digest()

    row = AiActReport(
        id=report_id,
        project_id=project.id,
        period_start=start,
        period_end=end,
        generated_at=generated_at,
        generated_by_user_id=requested_by.id,
        annex_json=annex_bytes.decode("utf-8"),
        annex_sha256=digest.hex(),
        annex_bytes=len(annex_bytes),
        # Over the 32 raw digest bytes, matching the recipe in section 5.
        signature=keystore.sign(digest),
        signing_kid=keystore.fingerprint(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


class ReportSummary(NamedTuple):
    """One history row, without the annex.

    A tuple of explicit columns rather than the ORM entity: selecting the
    entity loads ``annex_json`` for every row, and the history endpoint has no
    use for it. The type is the enforcement — there is no attribute here that
    could reach the blob by accident.
    """

    id: str
    project_id: str
    period_start: datetime
    period_end: datetime
    generated_at: datetime
    generated_by_user_id: str | None
    annex_sha256: str
    annex_bytes: int
    signature: bytes
    signing_kid: str


_SUMMARY_COLUMNS = (
    AiActReport.id,
    AiActReport.project_id,
    AiActReport.period_start,
    AiActReport.period_end,
    AiActReport.generated_at,
    AiActReport.generated_by_user_id,
    AiActReport.annex_sha256,
    AiActReport.annex_bytes,
    AiActReport.signature,
    AiActReport.signing_kid,
)

# Matches the audit reads' default page size. Reports accumulate without bound
# — nothing dedups on (project, period) — so this read is paginated like any
# other growing list rather than returning the whole history.
DEFAULT_HISTORY_LIMIT = 25
# Same ceiling the audit reads clamp to, so one page cannot be asked to carry
# the whole history.
MAX_HISTORY_LIMIT = 200


async def list_reports(
    session: AsyncSession,
    project_id: str,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
    offset: int = 0,
) -> list[ReportSummary]:
    """A page of a project's reports, newest first, without their annexes."""
    rows = (
        await session.exec(
            select(*_SUMMARY_COLUMNS)  # type: ignore[call-overload]
            .where(AiActReport.project_id == project_id)
            .order_by(AiActReport.generated_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [ReportSummary(*row) for row in rows]


async def get_report(
    session: AsyncSession, *, project_id: str, report_id: str
) -> AiActReport:
    """One report, scoped to its project.

    Scoped by project rather than fetched by primary key: an id from another
    tenant's project must read as absent, not as a 403 that confirms it exists.
    """
    row = (
        await session.exec(
            select(AiActReport).where(
                AiActReport.id == report_id,
                AiActReport.project_id == project_id,
            )
        )
    ).first()
    if row is None:
        raise ReportNotFound(report_id)
    return row


def annex_filename(report_id: str) -> str:
    """The annex's download name — the report id, so a saved file stays traceable."""
    return f"{report_id}-annex.json"
