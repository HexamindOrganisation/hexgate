"""mapping.py — span → validated event, per the semconv wire contract."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

import pytest

from hexgate.tracing import semconv
from hexgate_api.jobs.enricher.coerce import SpanRejected
from hexgate_api.jobs.enricher.mapping import map_span
from hexgate_api.schemas import (
    UINT32_MAX,
    BanEnforcementEvent,
    DecisionEvent,
    LlmInvocationEvent,
)
from tests.jobs.enricher.conftest import (
    ban_attrs,
    decision_attrs,
    make_span,
    usage_attrs,
)


def test_map_span_decision_happy_path() -> None:
    # 1.5ms past the second — must survive into DateTime64(3) precision.
    start_ns = (int(time.time()) - 60) * 10**9 + 1_500_000
    attrs = decision_attrs(
        **{
            semconv.USER_ROLES: ["admin", "dev"],
            semconv.DECIDING_ROLE: "admin",
            semconv.REASON: "matched allow rule",
            semconv.VIOLATIONS: ["v1"],
            semconv.ARGUMENTS: json.dumps({"query": "hello"}),
            semconv.SESSION_ID: "sess_1",
            semconv.USER_ID: "user_1",
        }
    )
    event = map_span(semconv.SCOPE_AUDIT, make_span(attrs, start_ns=start_ns), {})

    assert isinstance(event, DecisionEvent)
    assert str(event.event_id) == attrs[semconv.EVENT_ID]
    assert event.occurred_at == datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc)
    assert event.occurred_at.microsecond == 1_500  # ms precision preserved
    assert event.tool_name == "web_search"
    assert event.outcome == "allow"
    assert event.user_roles == ["admin", "dev"]
    assert event.arguments == {"query": "hello"}
    assert event.hint is None


def test_map_span_llm_happy_path() -> None:
    event = map_span(semconv.SCOPE_USAGE, make_span(usage_attrs()), {})
    assert isinstance(event, LlmInvocationEvent)
    assert event.model == "gpt-4o"
    assert event.input_tokens == 100
    assert event.output_tokens == 50
    assert event.latency_ms == 250
    assert event.status == "success"  # absent on the wire → model default


def test_map_span_ban_happy_path() -> None:
    event = map_span(semconv.SCOPE_BANS, make_span(ban_attrs()), {})
    assert isinstance(event, BanEnforcementEvent)
    assert event.ban_type == "agent"
    assert event.ban_id == "ban_123"


def test_when_scope_is_unknown_then_span_rejected() -> None:
    with pytest.raises(SpanRejected) as exc:
        map_span("some.other.scope", make_span(decision_attrs()), {})
    assert exc.value.error_class == "unknown_scope"


def test_when_event_id_is_not_a_uuid_then_span_rejected() -> None:
    attrs = decision_attrs(**{semconv.EVENT_ID: "not-a-uuid"})
    with pytest.raises(SpanRejected) as exc:
        map_span(semconv.SCOPE_AUDIT, make_span(attrs), {})
    assert exc.value.error_class == "validation"


def test_when_outcome_is_invalid_then_span_rejected() -> None:
    attrs = decision_attrs(**{semconv.OUTCOME: "maybe"})
    with pytest.raises(SpanRejected):
        map_span(semconv.SCOPE_AUDIT, make_span(attrs), {})


def test_when_arguments_json_is_invalid_then_span_rejected() -> None:
    attrs = decision_attrs(**{semconv.ARGUMENTS: "{broken"})
    with pytest.raises(SpanRejected):
        map_span(semconv.SCOPE_AUDIT, make_span(attrs), {})


def test_when_start_time_is_zero_then_span_rejected() -> None:
    with pytest.raises(SpanRejected) as exc:
        map_span(semconv.SCOPE_AUDIT, make_span(decision_attrs(), start_ns=0), {})
    assert "start_time" in exc.value.error


def test_when_occurred_at_is_in_the_future_then_span_rejected() -> None:
    future_ns = time.time_ns() + 10 * 60 * 10**9
    with pytest.raises(SpanRejected) as exc:
        map_span(
            semconv.SCOPE_AUDIT, make_span(decision_attrs(), start_ns=future_ns), {}
        )
    assert exc.value.error_class == "out_of_window"


def test_when_latency_exceeds_uint32_then_span_rejected() -> None:
    # A UInt32-overflowing value (e.g. latency emitted in ns) must be rejected
    # to the DLQ here, not fail permanently at ClickHouse insert time.
    attrs = usage_attrs(**{semconv.LATENCY_MS: UINT32_MAX + 1})
    with pytest.raises(SpanRejected) as exc:
        map_span(semconv.SCOPE_USAGE, make_span(attrs), {})
    assert exc.value.error_class == "validation"


def test_when_tokens_arrive_as_strings_then_coerced_to_int() -> None:
    attrs = usage_attrs(**{semconv.GEN_AI_USAGE_INPUT_TOKENS: "100"})
    event = map_span(semconv.SCOPE_USAGE, make_span(attrs), {})
    assert event.input_tokens == 100


def test_when_user_roles_arrive_as_json_string_then_coerced_to_list() -> None:
    attrs = decision_attrs(**{semconv.USER_ROLES: '["admin"]'})
    event = map_span(semconv.SCOPE_AUDIT, make_span(attrs), {})
    assert event.user_roles == ["admin"]


def test_when_latency_is_absent_then_derived_from_span_duration() -> None:
    attrs = usage_attrs()
    del attrs[semconv.LATENCY_MS]
    start_ns = time.time_ns() - 10**9
    span = make_span(attrs, start_ns=start_ns, end_ns=start_ns + 42_000_000)
    event = map_span(semconv.SCOPE_USAGE, span, {})
    assert event.latency_ms == 42


def test_when_agent_name_is_absent_then_resource_service_name_is_used() -> None:
    attrs = decision_attrs()
    del attrs[semconv.AGENT_NAME]
    event = map_span(
        semconv.SCOPE_AUDIT, make_span(attrs), {"service.name": "svc-agent"}
    )
    assert event.agent_name == "svc-agent"


# --- run attribution: the SDK↔platform contract -----------------------------


def test_map_span_carries_run_attribution() -> None:
    run_id = str(uuid.uuid4())
    attrs = decision_attrs(
        **{
            semconv.RUN_ID: run_id,
            semconv.RUN_TOOL_CALLS: 3,
            semconv.RUN_LLM_CALLS: 2,
            semconv.RUN_DENIALS: 1,
            semconv.RUN_TOTAL_TOKENS: 1200,
            semconv.RUN_ELAPSED_MS: 4500,
        }
    )

    event = map_span(semconv.SCOPE_AUDIT, make_span(attrs), {})

    assert str(event.run_id) == run_id
    assert event.run_tool_calls == 3
    assert event.run_llm_calls == 2
    assert event.run_denials == 1
    assert event.run_total_tokens == 1200
    assert event.run_elapsed_ms == 4500


def test_when_run_attributes_are_absent_then_counters_default_to_zero() -> None:
    """A span from an emitter that predates the run namespace still maps."""
    event = map_span(semconv.SCOPE_AUDIT, make_span(decision_attrs()), {})

    assert event.run_id is None
    assert event.run_tool_calls == 0
    assert event.run_elapsed_ms == 0


def test_sdk_decision_span_validates_in_and_out_of_a_run_scope() -> None:
    """The cross-package contract, asserted against the real SDK emitter — the
    only place both halves of the wire shape meet.

    Out of a run scope the SDK omits ``RUN_ID``: OTLP cannot carry null, and an
    empty string would DLQ the whole record rather than its attribution.
    """
    from hexgate.audit import AuditEvent
    from hexgate.security.decision import Decision, DecisionOutcome, RunAttribution

    run_id = str(uuid.uuid4())
    attributed = AuditEvent(
        decision=Decision(
            outcome=DecisionOutcome.DENY,
            agent_name="example_agent",
            tool_name="read_file",
            run=RunAttribution(
                run_id=run_id,
                tool_calls=3,
                llm_calls=2,
                denials=1,
                total_tokens=1200,
                elapsed_ms=4500,
            ),
        )
    ).span_attributes()
    detached = AuditEvent(
        decision=Decision(
            outcome=DecisionOutcome.ALLOW,
            agent_name="example_agent",
            tool_name="read_file",
        )
    ).span_attributes()

    in_run = map_span(semconv.SCOPE_AUDIT, make_span(attributed), {})
    out_of_run = map_span(semconv.SCOPE_AUDIT, make_span(detached), {})

    assert str(in_run.run_id) == run_id
    assert in_run.run_tool_calls == 3
    assert in_run.run_llm_calls == 2
    assert in_run.run_denials == 1
    assert in_run.run_total_tokens == 1200
    assert in_run.run_elapsed_ms == 4500
    assert out_of_run.run_id is None
    assert out_of_run.run_tool_calls == 0


def test_sdk_usage_span_validates_in_and_out_of_a_run_scope() -> None:
    """Same contract for llm_invocation.run_id, which joins usage rows to the
    policy_decision rows of the same invocation."""
    from hexgate.tracing.usage import LlmUsageEvent

    def _event(**overrides: object) -> LlmUsageEvent:
        base = dict(
            agent_name="a",
            model="gpt-4o",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            status="success",
        )
        return LlmUsageEvent(**{**base, **overrides})

    run_id = str(uuid.uuid4())
    attributed = map_span(
        semconv.SCOPE_USAGE, make_span(_event(run_id=run_id).span_attributes()), {}
    )
    detached = map_span(semconv.SCOPE_USAGE, make_span(_event().span_attributes()), {})

    assert str(attributed.run_id) == run_id
    assert detached.run_id is None
