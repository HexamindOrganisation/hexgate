# Seeds two tables: policy_decision, and the llm_message transcript each run
# came out of — the Audit drawer reads the second against the first, so a
# decision seeded without its conversation has an empty "LLM messages"
# section. Needs `make clickhouse-migrate` first on a database that predates
# migration 0003.
#
# Example: generate data for 40 users and 3 anomalies, project 00000000-0000-0000-0000-000000000003 and agent support-bot.
# cd platform/api && uv run python ../scripts/seed_audit.py --number_users 40 --number_anomalies 3 --project_id 00000000-0000-0000-0000-000000000003 --agent_name support-bot
#
# Example: clear seed rows for project 00000000-0000-0000-0000-000000000003.
# cd platform/api && uv run python ../scripts/seed_audit.py --clear --project_id 00000000-0000-0000-0000-000000000003

from __future__ import annotations

import hashlib
import json
import logging
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Tuple
from uuid import UUID, uuid4

from clickhouse_connect.driver.client import Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

sys.path.insert(0, str(Path(__file__).parent.parent / "api"))

# The sys.path bootstrap above has to run before these resolve.
from hexgate_api.constants import DEFAULT_PROJECT_ID  # noqa: E402
from hexgate_api.core.clickhouse import (  # noqa: E402
    BATCH_INSERT_SETTINGS,
    get_clickhouse,
)
from hexgate_api.features.audit.service import (  # noqa: E402
    _ANOMALY_MIN_REQUESTS,
    _DECISION_COLUMNS,
    _TIMEDELTA_ANOMALY_HOURS,
    _decision_row,
)
from hexgate_api.features.llm_messages.service import (  # noqa: E402
    _LLM_MESSAGE_COLUMNS,
    LLM_MESSAGE_TABLE,
    _llm_message_row,
)
from hexgate_api.schemas import (  # noqa: E402
    AuditOutcome,
    DecisionEvent,
    LlmMessageEvent,
)

from hexgate.audit import MAX_INPUT_MESSAGES_BYTES  # noqa: E402

# ── Agent & users ─────────────────────────────────────────────────────────────
# Uses the dev default project id (imported above) so data is visible in the dashboard.
# clear() scopes deletes to USER_IDS so real audit rows are not affected.

USER_IDS = [
    f"test_{name}"
    for name in [
        "Alice",
        "Bob",
        "Charlie",
        "Dave",
        "Eve",
        "Frank",
        "Grace",
        "Heidi",
        "Ivan",
        "Judy",
        "Karl",
        "Liam",
        "Mallory",
        "Nina",
        "Oscar",
        "Peggy",
        "Quentin",
        "Rupert",
        "Sybil",
        "Trent",
        "Uma",
        "Victor",
        "Wendy",
        "Xander",
        "Yara",
        "Zach",
        "Amber",
        "Brian",
        "Clara",
        "Derek",
        "Elena",
        "Felix",
        "Gina",
        "Hugo",
        "Iris",
        "Jack",
        "Kira",
        "Leo",
        "Maya",
        "Noah",
    ]
]

# ── Traffic shape ─────────────────────────────────────────────────────────────
# Normal: exactly 300 rows per user over 30 days, grouped into short runs (one
# session_id + one run_id each) spaced beyond one detector window, outcomes
# weighted 80% allow / 10% deny / 10% needs_approval. The grouping is what
# gives session_id and run_id meaning; see the run bounds below for why it
# must stay under the anomaly threshold.
# Anomaly: for each anomaly, one user (sampled from the seeded normal subset,
# USER_IDS[:number_users]) spikes 20-50 denies inside a single run in a
# 5-minute window at a random point in the last 30 days, probing restricted
# tools (refund_customer, create_ticket).
# Sampling from the seeded subset (not a fixed list) ensures the anomaly user
# already has normal-behavior rows, so the anomaly reads as a deviation rather
# than a user's only activity.

ROWS_PER_USER = 300
NUMBER_ANOMALIES = 1
REQUESTS_PER_ANOMALY_MIN = 20
REQUESTS_PER_ANOMALY_MAX = 50

SEED_WINDOW_DAYS = 30
SECONDS_BETWEEN_DECISIONS_MAX = 45
INGEST_LAG_SECONDS_MAX = 5

# Background traffic must not read as anomalous. The detector flags a user with
# _ANOMALY_MIN_REQUESTS or more decisions inside a _TIMEDELTA_ANOMALY_HOURS
# window at a >= 30% deny rate, so a normal run stays strictly below that
# request count and consecutive runs are spaced beyond one window — a run
# grouping that packed more decisions into a window than the threshold would
# make the seeded baseline flag ~140 medium anomalies on its own, drowning the
# deliberate spikes below. Bounds are derived from the detector's own
# constants so retuning it cannot silently reintroduce that.
DECISIONS_PER_RUN_MIN = 3
DECISIONS_PER_RUN_MAX = _ANOMALY_MIN_REQUESTS - 1
_MAX_RUN_DURATION = timedelta(
    seconds=(DECISIONS_PER_RUN_MAX - 1) * SECONDS_BETWEEN_DECISIONS_MAX
)
_MIN_RUN_SEPARATION = timedelta(hours=_TIMEDELTA_ANOMALY_HOURS) + _MAX_RUN_DURATION
TOKENS_PER_LLM_CALL_MIN = 300
TOKENS_PER_LLM_CALL_MAX = 4_000

OUTCOMES = [AuditOutcome.ALLOW, AuditOutcome.DENY, AuditOutcome.NEEDS_APPROVAL]
OUTCOME_WEIGHTS = [0.8, 0.1, 0.1]

TOOL_NAMES = ["refund_customer", "create_ticket", "read_customer", "web_search"]
RESTRICTED_TOOLS = ["refund_customer", "create_ticket"]

DENY_ERROR_TYPE = "permission_denied"
DENY_REASON = "User does not have permission"
DENY_VIOLATIONS = ["unauthorized_action"]
ANOMALY_VIOLATIONS = ["unauthorized_action", "policy_violation"]

# Role sets are per-user and stable, so the dashboard's by-role breakdown
# (which reads `has(user_roles, …)`) tells a consistent story across runs.
# `deciding_role` is empty on a deny — the schema's "every role denied".
ROLE_SETS = [
    ["support_agent"],
    ["support_agent", "billing_agent"],
    ["readonly"],
]

# Caller ABAC bags (ctx.*), seeded so the "Context attributes" drawer section
# has something to render. Deliberately non-PII — the same shape the docs use.
ATTRIBUTE_BAGS = [
    {"department": "finance", "region": "EU", "clearance_level": 3},
    {"department": "support", "region": "EU", "clearance_level": 1},
    {"department": "support", "region": "US", "clearance_level": 2},
]
ATTRIBUTES_POPULATED_RATIO = 0.66
# Platform-resolved from the agent registry in production; seeded rows belong
# to no registered agent version, and "" is what the column defaults to.
AGENT_VERSION_ID = ""
ANOMALY_ATTRIBUTES = {"department": "support", "region": "EU", "clearance_level": 1}

# ── Transcripts ───────────────────────────────────────────────────────────────
# Each run's decisions become one conversation. A model call ends with the
# tool call the decision is about, and the next call's input carries what the
# tool returned — so a run of N decisions has N+1 message events, one before
# each decision plus one for what followed the last. That is the shape the
# Audit drawer anchors on: the turn before the decision, and the turn after.

MODELS = ["gpt-5", "claude-opus-5"]
SYSTEM_PROMPT = (
    "You are a support agent for an online store. Use the tools you are given "
    "to look up orders and issue refunds. Never guess a customer's details."
)
# One case per run, so a transcript reads as a single task: the opening ask,
# the customer it is about, and the tools a support agent would actually
# reach for. Everything a call carries is derived from the case, because a
# transcript whose ask names one customer and whose calls name another says
# nothing about whether the decision on it was right.
CASES = [
    {
        "customer_id": 4417,
        "ask": "Customer #4417 says their refund never arrived — can you check?",
        "tools": ["read_customer", "refund_customer"],
        "query": "refund not received timeline",
    },
    {
        "customer_id": 8802,
        "ask": "Order 98120 shipped to the wrong address. What are my options?",
        "tools": ["read_customer", "create_ticket"],
        "query": "wrong shipping address policy",
    },
    {
        "customer_id": 2255,
        "ask": "Please close the ticket for customer #2255, they replied that it is fixed.",
        "tools": ["read_customer", "create_ticket"],
        "query": "closing a resolved ticket",
    },
    {
        "customer_id": 1043,
        "ask": "Has customer #1043 been charged twice this month?",
        "tools": ["read_customer", "refund_customer"],
        "query": "duplicate charge same month",
    },
]
FINAL_REPLIES = [
    "I have checked the account and summarised what I found above.",
    "That is everything I can do here — the rest needs a human reviewer.",
    "Done. I have recorded the outcome against the customer's record.",
]


def _tool_arguments(tool_name: str, case: Fields) -> Fields:
    """What the model would pass to this tool for this case.

    Per tool, not one shape for all of them: a search takes a query and no
    customer at all, and `web_search({"customer_id": …})` is nonsense on its
    face — the kind of thing that makes a seeded transcript unreadable as a
    story.
    """
    customer_id = case["customer_id"]
    if tool_name == "web_search":
        return {"query": case["query"]}
    if tool_name == "create_ticket":
        return {"customer_id": customer_id, "subject": "Follow-up"}
    if tool_name == "refund_customer":
        return {"customer_id": customer_id, "amount_cents": 2400}
    return {"customer_id": customer_id}


def _tool_result(tool_name: str, case: Fields) -> str:
    """What the tool returned, about the same customer the call named."""
    customer_id = case["customer_id"]
    if tool_name == "web_search":
        return json.dumps(
            {"results": [{"title": "Refund policy", "url": "https://help/refunds"}]}
        )
    if tool_name == "create_ticket":
        return json.dumps(
            {"ticket_id": 88213, "customer_id": customer_id, "status": "open"}
        )
    if tool_name == "refund_customer":
        return json.dumps(
            {"refund_id": "rf_9931", "customer_id": customer_id, "amount_cents": 2400}
        )
    return json.dumps({"id": customer_id, "plan": "pro", "open_tickets": 1})


# The model calls its tool a couple of seconds before the decision lands.
MESSAGE_LEAD_SECONDS = 2

# Deterministic imperfections, so each drawer marker has seeded data behind
# it rather than only a unit test. Counted over runs, not rows, because a
# marker is a property of one event inside a conversation.
GAP_EVERY_N_RUNS = 7
RESYNC_EVERY_N_RUNS = 5
TRUNCATE_EVERY_N_RUNS = 11
# Enough to look like a RAG call's retrieved context without approaching the
# real 256 KiB cap — the flag is what the drawer renders, not the size.
TRUNCATED_CONTEXT_CHARS = 3_000

# One deliberately long conversation per seed. Normal runs are capped at four
# decisions by the anomaly detector's threshold, so nothing else here crosses
# the drawer's 50-row page — the case where it has to fetch the transcript's
# tail instead of its head.
LONG_CONVERSATION_TURNS = 120
LONG_CONVERSATION_USER = USER_IDS[0]

# Two more one-off runs, each covering a shape the ordinary traffic cannot:
# a session with two message lists, and a turn whose completion asked for
# several tools at once.
HANDOFF_AGENT = "refunds-specialist"
HANDOFF_USER = USER_IDS[1]
# Its own agent name so the filter bar can reach it: there is no session
# filter, and hunting a session id by eye through the events table is the
# opposite of a usable example.
PARALLEL_AGENT = "case-resolver"
PARALLEL_USER = USER_IDS[2]

# A retrieval-augmented call, filled to the SDK's input cap. This is the only
# seeded row whose size is the point: the caps exist for RAG calls, where one
# message carries every retrieved chunk, and everything else here is a few
# hundred bytes. Sized off the constant rather than a fixed number of KiB so
# it tracks the cap if the cap moves.
RAG_AGENT = "policy-rag-agent"
RAG_USER = USER_IDS[3]
RAG_CHUNK_SOURCES = [
    "handbook/refunds.md",
    "handbook/shipping.md",
    "handbook/chargebacks.md",
    "policy/eu-consumer-rights.md",
    "runbook/duplicate-charges.md",
]
# Retrieved prose, varied per chunk so the rendered block reads like real
# retrieved context rather than a wall of one repeated line.
RAG_SENTENCES = [
    "A refund requested within 30 days of delivery is issued to the original "
    "payment method and settles in three to five business days.",
    "Where the customer paid by card and the card has since expired, the "
    "refund is issued as store credit and the customer is notified by email.",
    "Duplicate charges arising from a retried authorisation are reconciled "
    "nightly; the second charge is voided before it settles wherever possible.",
    "An order shipped to an address the customer did not enter is treated as "
    "a fulfilment error, and the replacement ships at no cost to the customer.",
    "Agents may not issue a refund above the order total, and any goodwill "
    "credit beyond that is recorded against the case for review.",
    "Under EU consumer law the customer may withdraw within fourteen days of "
    "delivery without giving a reason, and the trader refunds all payments "
    "received, including the standard cost of delivery.",
    "A chargeback opened before the refund settles is contested with the "
    "delivery record and the refund reference, and the case is held open "
    "until the issuer decides.",
]

# ── ClickHouse columns ────────────────────────────────────────────────────────
# Extends _DECISION_COLUMNS with received_at so the seed can control ingestion
# timestamps rather than letting ClickHouse stamp them at insert time.

_OCCURRED_AT = "occurred_at"
_idx = _DECISION_COLUMNS.index(_OCCURRED_AT)
_SEED_COLUMNS = (
    _DECISION_COLUMNS[: _idx + 1] + ["received_at"] + _DECISION_COLUMNS[_idx + 1 :]
)

# Same received_at splice for the message table, so a seeded transcript can be
# backdated with its decisions instead of all landing at insert time.
_MESSAGE_IDX = _LLM_MESSAGE_COLUMNS.index(_OCCURRED_AT)
_MESSAGE_SEED_COLUMNS = (
    _LLM_MESSAGE_COLUMNS[: _MESSAGE_IDX + 1]
    + ["received_at"]
    + _LLM_MESSAGE_COLUMNS[_MESSAGE_IDX + 1 :]
)

Row = list[Any]
Fields = dict[str, Any]


@dataclass(frozen=True, slots=True)
class SeedTarget:
    project_id: str
    agent_name: str


# ── Run attribution ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """The five run counters as of one decision, plus the run they belong to."""

    run_id: UUID
    tool_calls: int
    llm_calls: int
    denials: int
    total_tokens: int
    elapsed_ms: int

    def as_event_fields(self) -> Fields:
        """Only the run fields this build's DecisionEvent declares.

        As of this commit that is NONE of them: the six run_* fields exist
        only on the unmerged run-attribution branch, so against main this
        returns an empty dict and every seeded row takes ClickHouse's
        zero-run defaults. Keying off model_fields rather than hardcoding the
        six is what lets one script serve an API image from either side of
        that merge, with no edit needed when the fields land.
        """
        return {
            column: value
            for column, value in (
                ("run_id", self.run_id),
                ("run_tool_calls", self.tool_calls),
                ("run_llm_calls", self.llm_calls),
                ("run_denials", self.denials),
                ("run_total_tokens", self.total_tokens),
                ("run_elapsed_ms", self.elapsed_ms),
            )
            if column in DecisionEvent.model_fields
        }


class RunProgress:
    """Counters for one synthetic run.

    Snapshot-then-record, because the enforcer reads the run namespace before
    the decision it is about to make lands. A denial does not increment
    tool_calls — it must not consume a legitimate caller's budget
    (RunFacts.record_denial).
    """

    def __init__(self, rng: random.Random, run_id: UUID) -> None:
        self._rng = rng
        self._run_id = run_id
        self._tool_calls = 0
        self._llm_calls = 0
        self._denials = 0
        self._total_tokens = 0

    def snapshot(self, elapsed_ms: int) -> RunSnapshot:
        return RunSnapshot(
            run_id=self._run_id,
            tool_calls=self._tool_calls,
            llm_calls=self._llm_calls,
            denials=self._denials,
            total_tokens=self._total_tokens,
            elapsed_ms=elapsed_ms,
        )

    def record(self, outcome: AuditOutcome) -> None:
        """Apply one decision's effect, following RunFacts' record_* methods.

        Only an allow dispatches the tool, so only an allow is a tool_call. A
        denial is counted separately (record_denial) and an approval gate is
        counted as neither — "execution is counted separately, so an approval
        never granted consumes no budget" (RunFacts.record_approval), and
        approvals have no column here. The model was still called either way.
        """
        self._llm_calls += 1
        self._total_tokens += self._rng.randint(
            TOKENS_PER_LLM_CALL_MIN, TOKENS_PER_LLM_CALL_MAX
        )
        if outcome == AuditOutcome.DENY:
            self._denials += 1
        elif outcome == AuditOutcome.ALLOW:
            self._tool_calls += 1


# ── Row builders ──────────────────────────────────────────────────────────────
# Rows come from the API's own _decision_row, not from a local column list.
# That builder is the single place the row-shaping rules live (payload
# serialization, the falsy-attributes normalisation, the legacy role shim), so
# reusing it means the seed names no audit column at all beyond splicing
# received_at, and cannot drift from the ingest contract the way it did
# through the multi-role change (#120).


def _role_set(user_id: str) -> list[str]:
    return ROLE_SETS[USER_IDS.index(user_id) % len(ROLE_SETS)]


@dataclass(frozen=True, slots=True)
class SeededDecision:
    """What differs between a background decision and an anomaly probe.

    Everything else — the envelope, the deny fields, the role set — follows
    from these, so both traffic shapes go through one builder instead of two
    near-identical ones.
    """

    timestamp: datetime
    session_id: str
    user_id: str
    tool_name: str
    outcome: AuditOutcome
    violations: list[str]
    attributes: dict | None
    run: RunSnapshot
    # The case this run is working through (see CASES). Carried on the
    # decision so the transcript's arguments and tool results name the same
    # customer the ask did.
    case: Fields
    # Which agent made this call. None is the run's main agent; a handoff
    # target names itself, because its decisions and its message list are
    # both filed under its own name.
    agent_name: str | None = None


def _decision_seed_row(
    target: SeedTarget, rng: random.Random, decision: SeededDecision
) -> Row:
    """One policy_decision row, built by the API and given a seeded received_at.

    received_at is server-stamped in production, so it is absent from
    DecisionEvent and from _decision_row's output; splicing it in at the
    position _SEED_COLUMNS puts it is what lets the seed backdate ingestion
    instead of having every row land at insert time.
    """
    denied = decision.outcome == AuditOutcome.DENY
    roles = list(_role_set(decision.user_id))
    event = DecisionEvent(
        event_id=uuid4(),
        occurred_at=decision.timestamp,
        agent_name=decision.agent_name or target.agent_name,
        session_id=decision.session_id,
        user_id=decision.user_id,
        tool_name=decision.tool_name,
        outcome=decision.outcome,
        user_roles=roles,
        deciding_role="" if denied else roles[0],
        error_type=DENY_ERROR_TYPE if denied else "",
        reason=DENY_REASON if denied else "",
        violations=list(decision.violations),
        attributes=decision.attributes,
        **decision.run.as_event_fields(),
    )
    row = _decision_row(
        event, project_id=target.project_id, agent_version_id=AGENT_VERSION_ID
    )
    row.insert(
        _idx + 1,
        decision.timestamp + timedelta(seconds=rng.randint(0, INGEST_LAG_SECONDS_MAX)),
    )
    return row


def _background_decision(
    rng: random.Random,
    *,
    outcome: AuditOutcome,
    timestamp: datetime,
    session_id: str,
    user_id: str,
    run: RunSnapshot,
    case: Fields,
    agent_name: str | None = None,
    tool_name: str | None = None,
) -> SeededDecision:
    denied = outcome == AuditOutcome.DENY
    return SeededDecision(
        timestamp=timestamp,
        session_id=session_id,
        user_id=user_id,
        # From the case, not from TOOL_NAMES at large: the decision's tool is
        # what the transcript's completion asks for, so an unrelated draw
        # here is what made a run read as three errands for three customers.
        # web_search joins them so every tool still appears in the
        # dashboard's by-tool breakdown.
        tool_name=tool_name or rng.choice(case["tools"] + ["web_search"]),
        outcome=outcome,
        violations=DENY_VIOLATIONS if denied else [],
        # Absent a third of the time so the drawer's "omit when absent" path
        # shows up in the seeded data too, not just the populated one.
        attributes=(
            rng.choice(ATTRIBUTE_BAGS)
            if rng.random() < ATTRIBUTES_POPULATED_RATIO
            else None
        ),
        run=run,
        case=case,
        agent_name=agent_name,
    )


def _anomaly_decision(
    rng: random.Random,
    *,
    timestamp: datetime,
    session_id: str,
    user_id: str,
    run: RunSnapshot,
    case: Fields,
) -> SeededDecision:
    return SeededDecision(
        timestamp=timestamp,
        session_id=session_id,
        user_id=user_id,
        tool_name=rng.choice(RESTRICTED_TOOLS),
        outcome=AuditOutcome.DENY,
        violations=ANOMALY_VIOLATIONS,
        # Always populated: the anomaly is a restricted-tool deny, and the
        # low-clearance bag is what makes it explainable in the drawer.
        attributes=ANOMALY_ATTRIBUTES,
        run=run,
        case=case,
    )


# ── Transcript builders ───────────────────────────────────────────────────────
# Content is the OTel GenAI shape the adapters emit — a message is
# {role, parts} and a part names itself under `type` — because that is what
# the drawer renders. Reasoning is absent from every completion here, matching
# what the OpenAI adapter stores today (issue #221).


def _text(content: str) -> Fields:
    return {"type": "text", "content": content}


def _call_id(turn_key: str, seq: int, index: int = 0) -> str:
    """Identifies one call within one turn of one message list.

    The index is what keeps parallel calls apart: a completion asking for
    three tools at once produces three decisions, and the id is the only
    thing saying which decision belongs to which call.

    Keyed on the `turn_key`, not on the run id: a handoff puts two message
    lists in one session under one run, each counting `seq` from 0, so a
    run-keyed id would hand the same string to a call in each — and a reader
    matching a decision to its call by id inside a session has nothing left
    to tell them apart. Digested rather than spelled out so the id still
    reads like the opaque token a provider mints.
    """
    digest = hashlib.blake2s(turn_key.encode(), digest_size=4).hexdigest()
    return f"call_{digest}_{seq}_{index}"


def _tool_call_completion(
    group: list[SeededDecision], turn_key: str, seq: int
) -> list[Fields]:
    """The completion that asked for this turn's tools.

    One assistant message holding one `tool_call` part per decision, which
    is how a framework reports parallel calls and how ``output_messages``
    folds them back together.
    """
    return [
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "id": _call_id(turn_key, seq, index),
                    "name": decision.tool_name,
                    "arguments": json.dumps(
                        _tool_arguments(decision.tool_name, decision.case)
                    ),
                }
                for index, decision in enumerate(group)
            ],
        }
    ]


def _tool_result_messages(
    previous: list[SeededDecision], turn_key: str, seq: int
) -> list[Fields]:
    """One tool message per call the previous turn made.

    Parallel calls each return separately, so the next turn's input carries
    a result per call, matched to it by id.
    """
    return [
        _tool_result_message(d, turn_key, seq, index)
        for index, d in enumerate(previous)
    ]


def _tool_result_message(
    previous: SeededDecision, turn_key: str, seq: int, index: int = 0
) -> Fields:
    """What the framework fed back after the previous decision.

    One branch per outcome, because they are three different things and the
    model's next move is a reaction to which one it got. Folding
    needs_approval in with deny would put "permission_denied" in the
    transcript beside a decision row that says the call was gated for
    review — the transcript contradicting the record it exists to explain.
    """
    if previous.outcome == AuditOutcome.DENY:
        response = f"{DENY_ERROR_TYPE}: {DENY_REASON} ({previous.tool_name})"
    elif previous.outcome == AuditOutcome.NEEDS_APPROVAL:
        response = f"approval_required: {previous.tool_name} is held for human review"
    else:
        response = _tool_result(previous.tool_name, previous.case)
    return {
        "role": "tool",
        "parts": [
            {
                "type": "tool_call_response",
                "id": _call_id(turn_key, seq - 1, index),
                "response": response,
            }
        ],
    }


def _message_seed_row(
    target: SeedTarget,
    rng: random.Random,
    *,
    occurred_at: datetime,
    session_id: str,
    user_id: str,
    run_id: UUID,
    turn_key: str,
    agent_name: str,
    message_seq: int,
    model: str,
    input_messages: list[Fields],
    output_messages: list[Fields],
    system_instructions: list[Fields] | None,
    resynced: bool,
    truncated: bool,
) -> Row:
    """One llm_message row, built by the API's own row builder.

    Same reasoning as _decision_seed_row: the builder is where the row shape
    lives, so the seed names no column beyond splicing received_at in.
    """
    event = LlmMessageEvent(
        event_id=uuid4(),
        occurred_at=occurred_at,
        agent_name=agent_name,
        session_id=session_id,
        user_id=user_id,
        model=model,
        turn_key=turn_key,
        message_seq=message_seq,
        resynced=resynced,
        truncated=truncated,
        input_messages=json.dumps(input_messages),
        output_messages=json.dumps(output_messages),
        system_instructions=(
            json.dumps(system_instructions) if system_instructions else ""
        ),
        run_id=run_id,
    )
    row = _llm_message_row(
        event, project_id=target.project_id, agent_version_id=AGENT_VERSION_ID
    )
    row.insert(
        _MESSAGE_IDX + 1,
        occurred_at + timedelta(seconds=rng.randint(0, INGEST_LAG_SECONDS_MAX)),
    )
    return row


def _transcript_rows(
    target: SeedTarget,
    rng: random.Random,
    decisions: list[SeededDecision],
    run_index: int,
    *,
    groups: list[list[SeededDecision]] | None = None,
    agent_name: str | None = None,
    opens_conversation: bool = True,
) -> list[Row]:
    """One message list's conversation: N turns produce N+1 message events.

    A turn is a `group` — the decisions one completion asked for. Usually
    one, but a completion can request several tools at once, and then those
    decisions share a turn and anchor on it together.

    `turn_key` is the Hexgate run id plus the agent name, as the adapters
    build it. That is per message LIST, not per run: a handoff target keeps
    its own, restarting `message_seq` at 0, which is why gap detection has
    to be per `turn_key` rather than per session. `agent_name` names the
    agent whose list this is; `opens_conversation` is False for a handoff
    target, whose list starts from what it was handed rather than from the
    user's original ask.
    """
    if not decisions:
        return []
    groups = groups if groups is not None else [[d] for d in decisions]
    first = decisions[0]
    run_id = first.run.run_id
    agent = agent_name or target.agent_name
    turn_key = f"{run_id}:{agent}"
    model = rng.choice(MODELS)
    # One imperfection per run at most: overlapping them would leave no
    # seeded row showing a single marker on its own. `gap_at` is the turn
    # whose ROW the pipeline lost — never turn 0, which carries the system
    # instructions.
    gap_at = 1 if run_index % GAP_EVERY_N_RUNS == 0 and len(decisions) > 2 else None
    resync_at = 1 if gap_at is None and run_index % RESYNC_EVERY_N_RUNS == 0 else None
    truncate_at = (
        0
        if gap_at is None
        and resync_at is None
        and run_index % TRUNCATE_EVERY_N_RUNS == 0
        else None
    )

    rows: list[Row] = []
    seq = 0
    for index, group in enumerate(groups):
        decision = group[0]
        # The delta: the user's ask opens the conversation, every later call
        # carries only what came back from the tools the last one requested.
        if index == 0:
            new_input = (
                [{"role": "user", "parts": [_text(first.case["ask"])]}]
                if opens_conversation
                else [
                    {
                        "role": "user",
                        "parts": [
                            _text(
                                f"Handed off from {target.agent_name}: "
                                f"{first.case['ask']}"
                            )
                        ],
                    }
                ]
            )
        else:
            new_input = _tool_result_messages(groups[index - 1], turn_key, seq)
        if index == resync_at:
            # A resync restates the whole list rather than extending it.
            new_input = [
                {"role": "user", "parts": [_text(first.case["ask"])]}
            ] + new_input
        if index == truncate_at:
            new_input = new_input + [
                {
                    "role": "user",
                    "parts": [
                        _text("retrieved context " + "x" * TRUNCATED_CONTEXT_CHARS)
                    ],
                }
            ]
        row = _message_seed_row(
            target,
            rng,
            occurred_at=decision.timestamp - timedelta(seconds=MESSAGE_LEAD_SECONDS),
            session_id=decision.session_id,
            user_id=decision.user_id,
            run_id=run_id,
            turn_key=turn_key,
            agent_name=agent,
            message_seq=seq,
            model=model,
            input_messages=new_input,
            output_messages=_tool_call_completion(group, turn_key, seq),
            # First event of the turn_key only, as the adapters send it.
            system_instructions=[_text(SYSTEM_PROMPT)] if seq == 0 else None,
            resynced=index == resync_at,
            truncated=index == truncate_at,
        )
        # The lost event: built in full, then dropped. Everything it caused
        # stays — its seq is spent, its decision row is written, and the next
        # turn's input cites the result of a call whose own event is gone. A
        # dangling id is what a dropped span actually looks like; skipping
        # only the number would leave the conversation reading continuously,
        # which is the one thing a lost turn does not do.
        if index != gap_at:
            rows.append(row)
        seq += 1

    # What followed the last turn: its results fed back, and the model's
    # closing answer. Without this the drawer has nothing to show after the
    # run's final tool call.
    last = decisions[-1]
    rows.append(
        _message_seed_row(
            target,
            rng,
            occurred_at=last.timestamp + timedelta(seconds=MESSAGE_LEAD_SECONDS),
            session_id=last.session_id,
            user_id=last.user_id,
            run_id=run_id,
            turn_key=turn_key,
            agent_name=agent,
            message_seq=seq,
            model=model,
            input_messages=_tool_result_messages(groups[-1], turn_key, seq),
            output_messages=[
                {"role": "assistant", "parts": [_text(rng.choice(FINAL_REPLIES))]}
            ],
            system_instructions=None,
            resynced=False,
            truncated=False,
        )
    )
    return rows


def _validate_row_shape(rows: list[Row]) -> None:
    """Guard the one seam left between the seed and the API's row builder.

    _decision_row emits _DECISION_COLUMNS order and the splice puts
    received_at where _SEED_COLUMNS expects it, so a mismatch here means the
    ingest contract moved received_at or _idx no longer describes it — not a
    forgotten column, which is no longer possible to have.
    """
    widths = {len(row) for row in rows}
    if widths - {len(_SEED_COLUMNS)}:
        raise ValueError(
            f"seed row widths {sorted(widths)} != {len(_SEED_COLUMNS)} columns "
            f"({_SEED_COLUMNS}) — the audit row contract moved"
        )


def _validate_message_row_shape(rows: list[Row]) -> None:
    """Same seam guard as _validate_row_shape, for the message table."""
    widths = {len(row) for row in rows}
    if widths - {len(_MESSAGE_SEED_COLUMNS)}:
        raise ValueError(
            f"message row widths {sorted(widths)} != "
            f"{len(_MESSAGE_SEED_COLUMNS)} columns — the row contract moved"
        )


def _validate_columns(client: Client) -> None:
    """Fail before inserting if either table is behind its migration.

    llm_message ships as a hand-applied migration (0003), so a local
    ClickHouse that predates it has the decision table and not this one —
    which would otherwise surface as a mid-seed insert error with half the
    rows already written.
    """
    for table, columns in (
        ("policy_decision", _SEED_COLUMNS),
        (LLM_MESSAGE_TABLE, _MESSAGE_SEED_COLUMNS),
    ):
        result = client.query(f"DESCRIBE TABLE {table}")
        missing = set(columns) - {row[0] for row in result.result_rows}
        if missing:
            raise ValueError(
                f"ClickHouse table {table} is missing columns: {missing} "
                "— run `make clickhouse-migrate`"
            )


# ── Generators ────────────────────────────────────────────────────────────────


def _run_sizes(rng: random.Random, total: int) -> list[int]:
    """Partition ``total`` decisions into runs, each within the per-run bounds.

    The run count is drawn first and the total spread across it, rather than
    filling greedily: a greedy fill leaves a 1-2 decision remainder for most
    users, and folding that remainder into its neighbour would push one run
    to or past DECISIONS_PER_RUN_MAX + 1 — over the detector's request
    threshold, which is the one thing the bounds exist to prevent.
    """
    if total <= DECISIONS_PER_RUN_MAX:
        return [total]
    fewest = -(-total // DECISIONS_PER_RUN_MAX)
    most = max(total // DECISIONS_PER_RUN_MIN, fewest)
    count = rng.randint(fewest, most)
    size, larger = divmod(total, count)
    sizes = [size + 1] * larger + [size] * (count - larger)
    rng.shuffle(sizes)
    return sizes


def _past_instant(rng: random.Random, now: datetime) -> datetime:
    """A time inside the seed window, far enough back to stay in the past.

    Every row's received_at carries up to INGEST_LAG_SECONDS_MAX on top of
    its occurred_at, so a base drawn right up to ``now`` puts rows seconds
    into the future — which the dashboard's windows read as not-yet-happened.
    Reserving _MIN_RUN_SEPARATION at the recent end keeps that impossible.
    """
    span = timedelta(days=SEED_WINDOW_DAYS)
    latest = now - _MIN_RUN_SEPARATION
    return latest - timedelta(
        seconds=rng.uniform(0, (span - _MIN_RUN_SEPARATION).total_seconds())
    )


def _run_starts(rng: random.Random, now: datetime, count: int) -> list[datetime]:
    """One jittered start per run, one run per evenly-sized slot of the window.

    Slotting rather than a free scatter: two independently placed runs land in
    the same detector window often enough to flag the user, and jitter bounded
    by the slot minus _MIN_RUN_SEPARATION keeps consecutive runs more than a
    window apart by construction instead of by luck.
    """
    span = timedelta(days=SEED_WINDOW_DAYS)
    slot = span / count
    jitter_seconds = max((slot - _MIN_RUN_SEPARATION).total_seconds(), 0.0)
    earliest = now - span
    return [
        earliest + slot * index + timedelta(seconds=rng.uniform(0, jitter_seconds))
        for index in range(count)
    ]


def _run_timestamps(
    rng: random.Random, start: datetime, count: int
) -> Iterator[datetime]:
    timestamp = start
    for _ in range(count):
        yield timestamp
        timestamp += timedelta(seconds=rng.randint(1, SECONDS_BETWEEN_DECISIONS_MAX))


def _elapsed_ms(start: datetime, timestamp: datetime) -> int:
    return int((timestamp - start).total_seconds() * 1_000)


def generate_normal_data(
    target: SeedTarget,
    rng: random.Random,
    now: datetime,
    number_users: int,
) -> Tuple[list[Row], list[Row]]:
    """Background decisions and the conversations they came out of."""
    rows: list[Row] = []
    messages: list[Row] = []
    run_index = 0
    for user_id in USER_IDS[:number_users]:
        sizes = _run_sizes(rng, ROWS_PER_USER)
        for decisions, start in zip(sizes, _run_starts(rng, now, len(sizes))):
            session_id = str(uuid4())
            progress = RunProgress(rng, uuid4())
            case = rng.choice(CASES)
            outcomes = rng.choices(OUTCOMES, weights=OUTCOME_WEIGHTS, k=decisions)
            seeded: list[SeededDecision] = []
            for outcome, timestamp in zip(
                outcomes, _run_timestamps(rng, start, decisions)
            ):
                seeded.append(
                    _background_decision(
                        rng,
                        outcome=outcome,
                        timestamp=timestamp,
                        session_id=session_id,
                        user_id=user_id,
                        run=progress.snapshot(_elapsed_ms(start, timestamp)),
                        case=case,
                    )
                )
                progress.record(outcome)
            rows.extend(_decision_seed_row(target, rng, d) for d in seeded)
            messages.extend(_transcript_rows(target, rng, seeded, run_index))
            run_index += 1
    return rows, messages


def generate_anomalies(
    target: SeedTarget,
    rng: random.Random,
    now: datetime,
    number_anomalies: int,
    number_users: int,
) -> Tuple[list[Row], list[Row]]:
    """Deny spikes and the conversations they came out of."""
    rows: list[Row] = []
    messages: list[Row] = []
    for anomaly_index in range(number_anomalies):
        anomaly_user = rng.choice(USER_IDS[:number_users])
        anomaly_base = _past_instant(rng, now)
        requests = rng.randint(REQUESTS_PER_ANOMALY_MIN, REQUESTS_PER_ANOMALY_MAX)
        session_id = str(uuid4())
        progress = RunProgress(rng, uuid4())
        case = rng.choice(CASES)
        timestamps = sorted(
            anomaly_base - timedelta(minutes=rng.randint(0, 5)) for _ in range(requests)
        )
        start = timestamps[0]
        seeded: list[SeededDecision] = []
        for timestamp in timestamps:
            seeded.append(
                _anomaly_decision(
                    rng,
                    timestamp=timestamp,
                    session_id=session_id,
                    user_id=anomaly_user,
                    run=progress.snapshot(_elapsed_ms(start, timestamp)),
                    case=case,
                )
            )
            progress.record(AuditOutcome.DENY)
        rows.extend(_decision_seed_row(target, rng, d) for d in seeded)
        messages.extend(_transcript_rows(target, rng, seeded, anomaly_index))
    return rows, messages


def generate_long_conversation(
    target: SeedTarget, rng: random.Random, now: datetime
) -> Tuple[list[Row], list[Row]]:
    """One run long enough that its transcript does not fit a single page.

    Normal runs stop at four decisions (the anomaly threshold bounds them), so
    without this nothing seeded crosses the drawer's 50-row page and the path
    where it has to fetch the transcript's tail instead of its head is never
    taken. Mostly allows, spread over hours, so the detector does not read the
    volume as a spike.
    """
    start = now - timedelta(days=2)
    session_id = str(uuid4())
    progress = RunProgress(rng, uuid4())
    case = rng.choice(CASES)
    seeded: list[SeededDecision] = []
    for turn in range(LONG_CONVERSATION_TURNS):
        timestamp = start + timedelta(minutes=turn * 2)
        # One deny late in the conversation: the decision an auditor opens,
        # and the one whose anchor lies past the first page.
        outcome = (
            AuditOutcome.DENY
            if turn == LONG_CONVERSATION_TURNS - 8
            else AuditOutcome.ALLOW
        )
        seeded.append(
            _background_decision(
                rng,
                outcome=outcome,
                timestamp=timestamp,
                session_id=session_id,
                user_id=LONG_CONVERSATION_USER,
                run=progress.snapshot(_elapsed_ms(start, timestamp)),
                case=case,
            )
        )
        progress.record(outcome)
    rows = [_decision_seed_row(target, rng, d) for d in seeded]
    # run_index 1 keeps every marker off this run: its job is the page
    # boundary, and a gap in it would confuse which thing is being tested.
    return rows, _transcript_rows(target, rng, seeded, 1)


def generate_handoff_run(
    target: SeedTarget, rng: random.Random, now: datetime
) -> Tuple[list[Row], list[Row]]:
    """One run split across two agents, and so across two message lists.

    A handoff target keeps its own list: its `turn_key` differs and its
    `message_seq` restarts at 0. Nothing else seeded here has two lists in
    one session, so without this run the drawer's per-`turn_key` gap
    detection is only covered by a unit test — and the failure it guards
    against is silent, a sub-agent's first turn read as a pile of lost
    events.
    """
    start = now - timedelta(hours=6)
    session_id = str(uuid4())
    progress = RunProgress(rng, uuid4())
    case = rng.choice(CASES)

    def segment(
        count: int, offset: int, agent_name: str | None
    ) -> list[SeededDecision]:
        seeded: list[SeededDecision] = []
        for i in range(count):
            timestamp = start + timedelta(seconds=(offset + i) * 30)
            outcome = AuditOutcome.DENY if agent_name and i == 1 else AuditOutcome.ALLOW
            seeded.append(
                _background_decision(
                    rng,
                    outcome=outcome,
                    timestamp=timestamp,
                    session_id=session_id,
                    user_id=HANDOFF_USER,
                    run=progress.snapshot(_elapsed_ms(start, timestamp)),
                    case=case,
                    agent_name=agent_name,
                )
            )
            progress.record(outcome)
        return seeded

    main = segment(2, 0, None)
    # The specialist takes over mid-session and starts its own list.
    sub = segment(3, 2, HANDOFF_AGENT)

    rows = [_decision_seed_row(target, rng, d) for d in main + sub]
    # run_index 2 keeps both lists free of the seeded imperfections: this
    # run is about the list split, and a gap in it would confuse which of
    # the two things the drawer is showing.
    messages = _transcript_rows(target, rng, main, 2) + _transcript_rows(
        target,
        rng,
        sub,
        2,
        agent_name=HANDOFF_AGENT,
        opens_conversation=False,
    )
    return rows, messages


def generate_parallel_tool_calls_run(
    target: SeedTarget, rng: random.Random, now: datetime
) -> Tuple[list[Row], list[Row]]:
    """One completion asking for several tools at once.

    The framework returns them together, so the decisions share a timestamp
    and all anchor on the same turn — several decision rows, one "This
    call". Opening any of them should show that one turn, and the call id is
    the only thing saying which of the three the open decision is about.
    """
    start = now - timedelta(hours=3)
    session_id = str(uuid4())
    progress = RunProgress(rng, uuid4())
    case = rng.choice(CASES)

    def decision(timestamp: datetime, tool_name: str) -> SeededDecision:
        outcome = (
            AuditOutcome.DENY if tool_name == "refund_customer" else AuditOutcome.ALLOW
        )
        seeded = _background_decision(
            rng,
            outcome=outcome,
            timestamp=timestamp,
            session_id=session_id,
            user_id=PARALLEL_USER,
            run=progress.snapshot(_elapsed_ms(start, timestamp)),
            case=case,
            tool_name=tool_name,
            agent_name=PARALLEL_AGENT,
        )
        progress.record(outcome)
        return seeded

    # Turn 0 asks for one tool; turn 1 asks for three at once — same
    # timestamp, because one completion requested them together.
    opening = [decision(start, "read_customer")]
    fan_out_at = start + timedelta(seconds=40)
    fan_out = [
        decision(fan_out_at, tool)
        for tool in ("read_customer", "refund_customer", "create_ticket")
    ]

    groups = [opening, fan_out]
    seeded = opening + fan_out
    rows = [_decision_seed_row(target, rng, d) for d in seeded]
    return rows, _transcript_rows(
        target, rng, seeded, 2, groups=groups, agent_name=PARALLEL_AGENT
    )


def _rag_chunk(rng: random.Random, index: int) -> str:
    """One retrieved passage, cited the way a retriever hands it over."""
    source = RAG_CHUNK_SOURCES[index % len(RAG_CHUNK_SOURCES)]
    # Every sentence, shuffled: a retriever returns passages of roughly 500
    # tokens, and drawing a subset gave chunks a quarter of that size — the
    # cap would then be reached by implausibly many tiny ones.
    body = " ".join(rng.sample(RAG_SENTENCES, k=len(RAG_SENTENCES)))
    return f"[{source}#chunk-{index} score={rng.uniform(0.62, 0.94):.3f}]\n{body}"


def _rag_context_parts(rng: random.Random, budget_bytes: int) -> list[Fields]:
    """Retrieved chunks, filling the input budget without exceeding it.

    Measured on the serialized part, because the cap is in serialized-JSON
    bytes — the same units the SDK and the enricher enforce it in, and not
    the same as the characters you can count in the text.
    """
    parts: list[Fields] = []
    used = 0
    index = 0
    while True:
        part = _text(_rag_chunk(rng, index))
        addition = len(json.dumps(part).encode())
        if used + addition > budget_bytes:
            return parts
        parts.append(part)
        used += addition
        index += 1


def generate_rag_run(
    target: SeedTarget, rng: random.Random, now: datetime
) -> Tuple[list[Row], list[Row]]:
    """A retrieval-augmented call whose input sits at the cap.

    The caps were sized for exactly this call — one message carrying every
    retrieved chunk — and nothing else seeded here is within two orders of
    magnitude of them, so how the drawer renders a row of this size is
    otherwise untested outside production. The row is marked truncated
    because a real one arriving at the cap is one the SDK had to cut.
    """
    start = now - timedelta(hours=9)
    session_id = str(uuid4())
    progress = RunProgress(rng, uuid4())
    case = rng.choice(CASES)

    seeded: list[SeededDecision] = []
    for i, tool_name in enumerate(("read_customer", "refund_customer")):
        timestamp = start + timedelta(seconds=i * 45)
        # The refund is denied: a denial on a RAG call is the case an auditor
        # opens, and the retrieved context is what they have to read to judge
        # whether the model had grounds for asking.
        outcome = AuditOutcome.DENY if i == 1 else AuditOutcome.ALLOW
        seeded.append(
            _background_decision(
                rng,
                outcome=outcome,
                timestamp=timestamp,
                session_id=session_id,
                user_id=RAG_USER,
                run=progress.snapshot(_elapsed_ms(start, timestamp)),
                case=case,
                agent_name=RAG_AGENT,
                tool_name=tool_name,
            )
        )
        progress.record(outcome)

    rows = [_decision_seed_row(target, rng, d) for d in seeded]
    # run_index 2 keeps the ordinary imperfections off this run: its subject
    # is the size, and a gap or a resync on top would muddle which of the two
    # the drawer is showing.
    messages = _transcript_rows(target, rng, seeded, 2, agent_name=RAG_AGENT)
    # Refill turn 0's input with the retrieved context, at the cap, and mark
    # it truncated. Rebuilt rather than threaded through _transcript_rows: the
    # size is particular to this run, and the builder stays about the shape of
    # a conversation rather than about how big any one message is.
    cols = _MESSAGE_SEED_COLUMNS
    first = dict(zip(cols, messages[0]))
    context = _rag_context_parts(
        rng, MAX_INPUT_MESSAGES_BYTES - len(case["ask"].encode()) - 512
    )
    first["input_messages"] = json.dumps(
        [{"role": "user", "parts": [_text(case["ask"])] + context}]
    )
    first["truncated"] = 1
    messages[0] = [first[column] for column in cols]
    return rows, messages


# ── Public API ────────────────────────────────────────────────────────────────


def build_rows(
    target: SeedTarget, number_users: int, number_anomalies: int
) -> Tuple[list[Row], list[Row], list[Row]]:
    """(normal decisions, anomalous decisions, llm_message rows)."""
    now = datetime.now(timezone.utc)
    rng = random.Random(0)
    normal, normal_messages = generate_normal_data(target, rng, now, number_users)
    anomalous, anomaly_messages = generate_anomalies(
        target, rng, now, number_anomalies, number_users
    )
    long_rows, long_messages = generate_long_conversation(target, rng, now)
    handoff_rows, handoff_messages = generate_handoff_run(target, rng, now)
    parallel_rows, parallel_messages = generate_parallel_tool_calls_run(
        target, rng, now
    )
    rag_rows, rag_messages = generate_rag_run(target, rng, now)
    return (
        normal + long_rows + handoff_rows + parallel_rows + rag_rows,
        anomalous,
        normal_messages
        + anomaly_messages
        + long_messages
        + handoff_messages
        + parallel_messages
        + rag_messages,
    )


def _validate_seed_args(number_users: int, number_anomalies: int) -> Tuple[int, int]:
    if number_users < 1:
        logging.info("number_users < 1, defaulting to 1")
        number_users = 1
    elif number_users > len(USER_IDS):
        logging.info(
            "number_users > %d, defaulting to %d", len(USER_IDS), len(USER_IDS)
        )
        number_users = len(USER_IDS)
    if number_anomalies < 0:
        logging.info("number_anomalies < 0, defaulting to 0")
        number_anomalies = 0
    elif number_anomalies > 100:
        logging.info("number_anomalies > 100, defaulting to 100")
        number_anomalies = 100
    return number_users, number_anomalies


def seed(
    client: Client,
    target: SeedTarget,
    number_users: int,
    number_anomalies: int,
) -> Tuple[int, int, int]:
    clear(client, target.project_id)
    normal, anomalous, messages = build_rows(target, number_users, number_anomalies)
    _validate_row_shape(normal + anomalous)
    _validate_message_row_shape(messages)
    client.insert(
        "policy_decision",
        normal + anomalous,
        column_names=_SEED_COLUMNS,
        settings=BATCH_INSERT_SETTINGS,
    )
    client.insert(
        LLM_MESSAGE_TABLE,
        messages,
        column_names=_MESSAGE_SEED_COLUMNS,
        settings=BATCH_INSERT_SETTINGS,
    )
    return len(normal), len(anomalous), len(messages)


def clear(client: Client, project_id: str) -> None:
    """Delete seed rows from both tables.

    Scoped to USER_IDS, so real audit rows are untouched. The transcripts go
    with the decisions they explain — leaving them behind would make the
    drawer show a conversation for a decision that no longer exists.
    """
    for table in ("policy_decision", LLM_MESSAGE_TABLE):
        client.command(
            f"ALTER TABLE {table} DELETE WHERE user_id IN {{users:Array(String)}} "
            "AND project_id = {pid:String}",
            parameters={"users": USER_IDS, "pid": project_id},
            settings={"mutations_sync": "2"},
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Seed ClickHouse with audit test data."
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete seed rows instead of inserting.",
    )
    parser.add_argument(
        "--agent_name",
        default="support-agent",
        help="Agent name to use for seed rows.",
    )
    parser.add_argument(
        "--project_id",
        default=DEFAULT_PROJECT_ID,
        help="Project ID to use for seed rows.",
    )
    parser.add_argument(
        "--number_users",
        type=int,
        default=3,
        help="Number of distinct users to draw from (max 40).",
    )
    parser.add_argument(
        "--number_anomalies",
        type=int,
        default=NUMBER_ANOMALIES,
        help="Number of anomaly spikes to generate.",
    )
    args = parser.parse_args()

    client = get_clickhouse()

    if args.clear:
        clear(client, args.project_id)
        print("Seed rows cleared.")
    else:
        _validate_columns(client)
        args.number_users, args.number_anomalies = _validate_seed_args(
            args.number_users, args.number_anomalies
        )
        normal_count, anomalous_count, message_count = seed(
            client,
            SeedTarget(project_id=args.project_id, agent_name=args.agent_name),
            args.number_users,
            args.number_anomalies,
        )
        print(
            f"Inserted {normal_count + anomalous_count} decision rows "
            f"({normal_count} normal + {anomalous_count} anomalous) "
            f"and {message_count} llm_message rows."
        )
