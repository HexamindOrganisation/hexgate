"""OTLP pipeline smoke test: one of every event type, then verify they landed.

Sends eleven events through the SDK's sender (configure -> emit -> flush) to
the stage HEXGATE_API_URL / HEXGATE_API_KEY point at:

    policy decision  allow / deny / needs_approval   -> policy_decision table
    LLM usage                                        -> llm_invocation table
    ban enforcement                                  -> ban_enforcement table
    LLM messages     one small, five over the cap    -> llm_message table

All eleven ride the same sender, tagged with a per-run session id so the
verification step can find exactly this run's rows.

The oversized message events are what makes this the check that the pipeline's
record-size budget (docs/internals/audit-pipeline.md §4.1) was actually
DEPLOYED, and not merely merged. Raising those limits is an operational
change — topics altered, collector and enricher restarted — and everything
else here is small enough to ride a stage that never got it. Five capped
message spans are not: together they overflow the 1 MB record the collector
and the broker default to, so on a stage still at those defaults the export is
rejected whole and every event reports MISSING, while on a raised stage the
same ~1.25 MiB sits comfortably inside the 8 MiB record. They double as the only
check that a payload past the SDK cap is degraded rather than dropped, which
is the whole point of capping head+tail instead of rejecting.

They also make the send step need real uplink: ~1.3 MB in ONE uncompressed
POST, and the exporter's 5 s deadline covers the whole request including its
retries, so below ~2 Mbit/s the body never finishes and the batch is dropped.
That failure reports as every event MISSING — the same shape as an undeployed
stage — so read the traceback before believing the stage is at fault.

Scope — what this does and does not prove. It starts at the sender, so it
covers the transport and storage path: OTLP exporter -> reverse proxy ->
Collector (auth, project key) -> Redpanda -> span-enricher -> ClickHouse ->
dashboard read endpoints. It runs no agent: the PolicyEnforcer, the adapter
hooks that produce usage and ban events, identity from the HexgateContext
scope, and policy evaluation are all upstream of where this starts and are
covered by the adapter integration tests (`pytest -m integration`), not here.
A PASS means "a correctly-formed span from this SDK version lands and is
queryable on this stage"; it does not mean "an agent on this stage is audited".

Verification needs a dashboard login for a project admin (the ban-enforcement
read is admin-gated; an API key is not enough for any of them). Set
HEXGATE_SMOKE_EMAIL and HEXGATE_SMOKE_PASSWORD to that account and the script
polls the API until every row shows up. Without them it prints the ClickHouse
queries to run on the box instead.

Usage (the post-deploy check in platform/DEPLOY.md §4; the Makefile target
maps STAGE to the stage's origin, or set HEXGATE_API_URL yourself):

    export HEXGATE_API_KEY=fty_live_...          # minted on THAT stage — a prod
                                                 # key 401s on staging's collector
    export HEXGATE_SMOKE_EMAIL=you@example.com HEXGATE_SMOKE_PASSWORD=...
    make platform-smoke STAGE=staging

Exit codes: 0 every event landed, 1 some did not, 2 nothing was verified
because no credentials were set. Sending alone proves nothing, hence the
distinct 2: the OTLP exporter never raises on transport or auth failures, it
logs them at ERROR, so any traceback above the verification step is a failure
even though the script keeps going. Two spellings to look for, because they
mean different things: "Failed to export" is the collector refusing the batch
(auth, size, a 4xx), while "Exception while exporting Span … TimeoutError" is
this machine not finishing the upload inside the exporter's 5 s deadline — see
the note on the oversized events above.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from uuid import uuid4

import httpx

from hexgate import audit
from hexgate.audit import MAX_INPUT_MESSAGES_BYTES, AuditEvent
from hexgate.cloud.biscuit import parse_envelope
from hexgate.config.env import resolve_api_url, resolve_otlp_endpoint
from hexgate.security.bans import BanEnforcementEvent
from hexgate.security.decision import Decision, DecisionOutcome
from hexgate.tracing.messages import LlmMessageEvent
from hexgate.tracing.usage import LlmUsageEvent

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

AGENT_NAME = "otlp_smoke"
POLL_TIMEOUT_S = 60
POLL_INTERVAL_S = 3

# The oversized events' single input message, sized off the SDK cap rather
# than as a fixed number of KiB: what makes one worth sending is that it lands
# *past* whatever the cap currently is, so the row must come back marked
# truncated. After capping, each leaves the SDK at roughly the cap itself.
OVERSIZED_INPUT_BYTES = MAX_INPUT_MESSAGES_BYTES + 16 * 1024

# What the record-size budget was raised FROM. Two different numbers, and the
# record has to clear BOTH to prove anything: the kafka exporter checks
# configkafka's default max_message_bytes of 1 000 000 before the broker sees
# the record at all, and the broker's own max.message.bytes defaults to 1 MiB
# (1 048 576) — the larger of the two, so it is the one to size against.
LEGACY_MAX_RECORD_BYTES = 1024 * 1024

# Enough capped message spans to overflow that record. One would not: at
# ~256 KiB it rides either limit untouched, so a single oversized event
# verifies the SDK cap and proves nothing about the stage. The floor division
# counts how many FIT — exactly four, since the cap divides the limit evenly,
# which means four land ON the broker's line rather than past it — so the +1
# is what crosses it, and it crosses by 25% (five spans, ~1.25 MiB), margin
# enough that the protobuf framing and the other six spans are not load-bearing.
OVERSIZED_EVENT_COUNT = LEGACY_MAX_RECORD_BYTES // MAX_INPUT_MESSAGES_BYTES + 1

# Floor on what an OVERSIZED row's input content may weigh once it lands. The
# SDK caps it to exactly MAX_INPUT_MESSAGES_BYTES, so half of that is slack no
# healthy row uses; it fires only when some hop clipped the content on the way
# through. No assertion on the `truncated` flag can catch that, since a row
# gutted to nothing still carries the flag.
MIN_LANDED_INPUT_BYTES = MAX_INPUT_MESSAGES_BYTES // 2


def build_events(session_id: str, user_id: str) -> dict[str, object]:
    """One event per pipeline path, keyed by a label used in the report."""

    def decision(
        outcome: DecisionOutcome, tool: str, granted: str | None
    ) -> AuditEvent:
        d = Decision(
            outcome=outcome,
            agent_name=AGENT_NAME,
            tool_name=tool,
            user_roles=("analyst",),
            deciding_role=granted,
            reason=f"otlp smoke test ({outcome.value})",
        )
        return AuditEvent(decision=d, user_id=user_id, session_id=session_id)

    # One message list for this run, so the events form a single turn the read
    # endpoint returns in message_seq order — which also makes the sequence
    # contiguous, the shape a reader uses to tell a complete transcript from
    # one the pipeline dropped a row out of.
    turn_key = f"turn_{session_id}"

    # The GenAI part shape the adapters' text_part() produces, written out
    # here rather than imported: hexgate.adapters.openai pulls the agents SDK
    # in, which this script has no other reason to need.
    def text_part(content: str) -> dict[str, str]:
        return {"type": "text", "content": content}

    # Built and sent directly, like every other event here, so the transport
    # is checked even on a stage whose operators set HEXGATE_LOG_MESSAGES=0 —
    # that switch is about what an agent records, not whether the pipeline
    # carries it.
    def message(seq: int, prompt: str) -> LlmMessageEvent:
        return LlmMessageEvent(
            agent_name=AGENT_NAME,
            model="smoke-model",
            input_messages=[{"role": "user", "parts": [text_part(prompt)]}],
            output_messages=[
                {"role": "assistant", "parts": [text_part(f"otlp smoke reply {seq}")]}
            ],
            # First event of the turn only, as the adapters emit it.
            system_instructions=[text_part("otlp smoke system prompt")]
            if seq == 0
            else None,
            turn_key=turn_key,
            message_seq=seq,
            session_id=session_id,
            user_id=user_id,
        )

    return {
        "decision:allow": decision(DecisionOutcome.ALLOW, "search_docs", "analyst"),
        "decision:deny": decision(DecisionOutcome.DENY, "read_file", None),
        "decision:needs_approval": decision(
            DecisionOutcome.NEEDS_APPROVAL, "send_email", None
        ),
        "llm_usage": LlmUsageEvent(
            agent_name=AGENT_NAME,
            model="smoke-model",
            input_tokens=12,
            output_tokens=4,
            latency_ms=42,
            status="success",
            session_id=session_id,
            user_id=user_id,
        ),
        "ban_enforcement": BanEnforcementEvent(
            ban_type="agent",
            ban_id="ban_smoke",
            agent_name=AGENT_NAME,
            reason="otlp smoke test",
            user_id=user_id,
            session_id=session_id,
        ),
        "llm_message": message(0, "otlp smoke prompt"),
        # RAG-shaped calls: each message carries more retrieved text than the
        # cap allows. Truncation happens SDK-side, so each of these is the
        # biggest span the pipeline is ever asked to carry.
        **{
            f"llm_message:oversized{seq}": message(seq, "x" * OVERSIZED_INPUT_BYTES)
            for seq in range(1, OVERSIZED_EVENT_COUNT + 1)
        },
    }


def send(events: dict[str, object]) -> None:
    sender = audit.configure()
    if sender is None:
        raise SystemExit(
            "no audit sender: key not resolvable or HEXGATE_LOCAL_MODE is set"
        )
    for ev in events.values():
        sender.emit(ev)  # type: ignore[arg-type]  # every event class satisfies SpanEvent
    asyncio.run(audit.shutdown())  # flushes the batch; export errors log above


def login(client: httpx.Client, email: str, password: str) -> None:
    # FastAPI Users' cookie backend: OAuth2 password form, sets hexgate_session.
    r = client.post(
        "/v1/auth/cookie/login", data={"username": email, "password": password}
    )
    if r.status_code >= 400:
        raise SystemExit(f"dashboard login failed: {r.status_code} {r.text}")


def _read(client: httpx.Client, path: str, **params: str | int) -> dict:
    """GET a read endpoint; raises HTTPStatusError for verify()'s retry logic."""
    r = client.get(path, params=params)
    r.raise_for_status()
    return r.json()


def verify(
    client: httpx.Client,
    project_id: str,
    session_id: str,
    user_id: str,
    events: dict[str, object],
) -> bool:
    """Poll the dashboard read endpoints until every event is visible or time runs out."""
    want_decisions = {
        str(ev.event_id): ev.decision.outcome.value  # type: ignore[attr-defined]
        for label, ev in events.items()
        if label.startswith("decision:")
    }
    ban_event_id = str(events["ban_enforcement"].event_id)  # type: ignore[attr-defined]
    want_messages = {
        label: ev for label, ev in events.items() if isinstance(ev, LlmMessageEvent)
    }
    base = f"/v1/projects/{project_id}"

    deadline = time.monotonic() + POLL_TIMEOUT_S
    missing: dict[str, str] = {}
    while True:
        missing = {}

        try:
            rows = _read(
                client,
                f"{base}/audit/decisions",
                agent=AGENT_NAME,
                session_id=session_id,
                limit=50,
            )["rows"]
            seen = {r["event_id"]: r["outcome"] for r in rows}
            for eid, outcome in want_decisions.items():
                if eid not in seen:
                    missing[f"decision:{outcome}"] = "row not found"
                elif seen[eid] != outcome:
                    missing[f"decision:{outcome}"] = (
                        f"landed with outcome {seen[eid]!r}"
                    )

            bans = _read(client, f"{base}/audit/ban-enforcements", limit=100)["rows"]
            if not any(r["event_id"] == ban_event_id for r in bans):
                missing["ban_enforcement"] = "row not found"

            # No per-row read for usage; the summary filtered on this run's unique
            # user id is exactly one call when the event landed.
            totals = _read(
                client, f"{base}/llm/summary", agent=AGENT_NAME, user=user_id
            )["totals"]
            if totals["calls"] < 1:
                missing["llm_usage"] = "no calls in summary"

            # The transcript read is session-scoped, so this run's six rows are
            # the whole page. Checking turn_key and message_seq is the point:
            # they are what makes a set of rows a readable conversation, and a
            # mapping bug in the enricher would land the row with either blank.
            transcript = _read(
                client, f"{base}/audit/llm-messages", session_id=session_id, limit=50
            )["rows"]
            by_event_id = {r["event_id"]: r for r in transcript}
            for label, ev in want_messages.items():
                row = by_event_id.get(str(ev.event_id))
                if row is None:
                    missing[label] = "row not found"
                elif row["turn_key"] != ev.turn_key:
                    missing[label] = f"turn_key landed as {row['turn_key']!r}"
                elif row["message_seq"] != ev.message_seq:
                    missing[label] = f"message_seq landed as {row['message_seq']}"
                # Degraded, not dropped: the oversized events must arrive with
                # the flag set, and the ordinary one must NOT — a cap that
                # fired on a few hundred bytes would make the flag meaningless.
                elif row["truncated"] != label.startswith("llm_message:oversized"):
                    missing[label] = f"truncated landed as {row['truncated']}"
                # Size, not just the flag. `truncated` is the SDK's cut OR'd
                # with the enricher's, so a row clipped to nothing by a hop in
                # between still carries it and would pass every check above.
                # What the oversized events exist to prove is that ~256 KiB
                # survives the pipeline *at that size*, so read the size back.
                elif label != "llm_message" and (
                    len(json.dumps(row["input_messages"])) < MIN_LANDED_INPUT_BYTES
                ):
                    landed = len(json.dumps(row["input_messages"]))
                    missing[label] = f"content clipped to {landed} bytes"
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status < 500:
                # e.g. 403 on ban-enforcements when the account is not a project
                # admin — polling will never fix that, so say what happened.
                raise SystemExit(
                    f"read {exc.request.url.path} -> {status}: {exc.response.text[:200]}"
                ) from exc
            # 5xx, typically the api's 503 while ClickHouse warms up after a
            # platform-up: that is what the poll window is for, so keep going.
            missing = dict.fromkeys(events, f"read failed: {status}")

        if not missing or time.monotonic() > deadline:
            break
        time.sleep(POLL_INTERVAL_S)

    for label in events:
        status = "MISSING  " + missing[label] if label in missing else "ok"
        print(f"  {label:<24} {status}")
    return not missing


def main() -> int:
    api_key = os.environ.get("HEXGATE_API_KEY")
    if not api_key:
        print("HEXGATE_API_KEY is not set", file=sys.stderr)
        return 1
    _, project_id, _ = parse_envelope(api_key)
    api_url = resolve_api_url()

    run = uuid4().hex[:8]
    session_id = f"s_smoke_{run}"
    user_id = f"u_smoke_{run}"
    events = build_events(session_id, user_id)

    print(
        f"exporting {len(events)} events to {resolve_otlp_endpoint()}  (session {session_id})"
    )
    send(events)
    print("flush complete (an ERROR line above means the collector rejected the batch)")

    email = os.environ.get("HEXGATE_SMOKE_EMAIL")
    password = os.environ.get("HEXGATE_SMOKE_PASSWORD")
    if not (email and password):
        print(
            "\nHEXGATE_SMOKE_EMAIL / HEXGATE_SMOKE_PASSWORD not set; verify on the box:\n"
            f"  clickhouse-client --query \"SELECT outcome, tool_name FROM hexgate_audit.policy_decision WHERE session_id = '{session_id}'\"\n"
            f"  clickhouse-client --query \"SELECT model, input_tokens FROM hexgate_audit.llm_invocation WHERE session_id = '{session_id}'\"\n"
            f"  clickhouse-client --query \"SELECT ban_type, ban_id FROM hexgate_audit.ban_enforcement WHERE session_id = '{session_id}'\"\n"
            f"  clickhouse-client --query \"SELECT turn_key, message_seq, truncated FROM hexgate_audit.llm_message WHERE session_id = '{session_id}' ORDER BY message_seq\"\n"
            "  (prefix each with: docker exec hexgate-<stage>-clickhouse-1)"
        )
        print("\nSKIPPED: nothing was verified (no dashboard credentials)")
        return 2

    print(f"\nverifying via {api_url} as {email} (up to {POLL_TIMEOUT_S}s)")
    with httpx.Client(base_url=api_url, timeout=15) as client:
        login(client, email, password)
        ok = verify(client, project_id, session_id, user_id, events)
    print("\nPASS: all events landed" if ok else "\nFAIL: some events did not land")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
