"""End-to-end _process_poll against a real local ClickHouse.

Real protobuf decode, real batch inserts, fake Kafka clients — the broker
round-trip belongs to the staging wire-up. Opt-in via `pytest -m integration`
(`make platform-api-test-integration`).
"""

from __future__ import annotations

import uuid

import pytest

from hexgate.tracing import semconv
from hexgate_api.jobs.enricher.consumer import EnricherJob
from hexgate_api.settings import Settings
from tests.jobs.enricher.conftest import (
    FakeConsumer,
    FakeProducer,
    FakeRecord,
    ban_attrs,
    decision_attrs,
    make_request_bytes,
    make_span,
    message_attrs,
    run_attrs,
    usage_attrs,
)

_TABLES = ("policy_decision", "llm_invocation", "ban_enforcement", "llm_message")


def _count(client, table: str, project_id: str) -> int:
    return client.query(
        f"SELECT count() FROM {table} FINAL WHERE project_id = {{pid:String}}",
        parameters={"pid": project_id},
    ).result_rows[0][0]


def _job(monkeypatch: pytest.MonkeyPatch, client) -> EnricherJob:
    """A job on the real ClickHouse client, fake Kafka, stubbed version resolve."""

    async def _stub_resolve(pairs):
        return {pair: "ver_int" for pair in pairs}

    monkeypatch.setattr(
        "hexgate_api.jobs.enricher.consumer.resolve_versions", _stub_resolve
    )
    calls: list[str] = []
    return EnricherJob(
        Settings(enricher_insert_max_backoff_s=0.01),
        clickhouse_client=client,
        consumer=FakeConsumer(calls),
        producer=FakeProducer(calls),
    )


def _delete_project(client, tables: tuple[str, ...], project_id: str) -> None:
    for table in tables:
        client.command(
            f"ALTER TABLE {table} DELETE WHERE project_id = {{pid:String}}",
            parameters={"pid": project_id},
        )


@pytest.mark.integration
async def test_process_poll_round_trip_and_reprocess_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hexgate_api.core.clickhouse import get_clickhouse

    client = get_clickhouse()
    project_id = f"test_proj_{uuid.uuid4().hex[:8]}"

    job = _job(monkeypatch, client)
    records = [
        FakeRecord(
            key=project_id.encode(),
            value=make_request_bytes(
                [
                    (semconv.SCOPE_AUDIT, [make_span(decision_attrs())]),
                    (semconv.SCOPE_USAGE, [make_span(usage_attrs())]),
                    (semconv.SCOPE_BANS, [make_span(ban_attrs())]),
                    (semconv.SCOPE_MESSAGES, [make_span(message_attrs())]),
                ]
            ),
        )
    ]
    try:
        await job._process_poll(records)
        assert [_count(client, t, project_id) for t in _TABLES] == [1, 1, 1, 1]

        # Reprocessing the same records (crash-before-commit replay) must not
        # double-count: event_id dedup via ReplacingMergeTree, FINAL applies
        # merge semantics at read time.
        await job._process_poll(records)
        assert [_count(client, t, project_id) for t in _TABLES] == [1, 1, 1, 1]
    finally:
        _delete_project(client, _TABLES, project_id)


_USAGE_SOURCES = ("policy_decision", "llm_invocation", "agent_run")
# invocations, tool_calls, denials, llm_calls, input_tokens, output_tokens
_EXPECTED_USAGE = (1, 2, 1, 1, 100, 50)


def _usage(client, project_id: str) -> tuple:
    return tuple(
        client.query(
            "SELECT sum(invocations), sum(tool_calls), sum(denials), "
            "sum(llm_calls), sum(input_tokens), sum(output_tokens) "
            "FROM usage_minute WHERE project_id = {pid:String}",
            parameters={"pid": project_id},
        ).result_rows[0]
    )


@pytest.mark.integration
async def test_a_redelivered_poll_is_not_summed_twice_into_usage_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 end to end: a view sums every inserted copy, so the cross-poll
    dedup is what keeps a redelivered span from counting twice. Needs
    migration 0005 on the local volume (`make clickhouse-migrate`)."""
    from hexgate_api.core.clickhouse import get_clickhouse

    client = get_clickhouse()
    project_id = f"test_proj_{uuid.uuid4().hex[:8]}"
    job = _job(monkeypatch, client)
    decisions = [
        make_span(decision_attrs()),
        make_span(decision_attrs(**{semconv.OUTCOME: "deny"})),
        make_span(decision_attrs(**{semconv.OUTCOME: "needs_approval"})),
        # Egress is decided outside the guard runner: dropped.
        make_span(decision_attrs(**{semconv.TOOL_NAME: "net.http_request"})),
    ]
    records = [
        FakeRecord(
            key=project_id.encode(),
            value=make_request_bytes(
                [
                    (semconv.SCOPE_RUNS, [make_span(run_attrs())]),
                    (semconv.SCOPE_AUDIT, decisions),
                    (semconv.SCOPE_USAGE, [make_span(usage_attrs())]),
                ]
            ),
        )
    ]
    try:
        await job._process_poll(records)
        assert _usage(client, project_id) == _EXPECTED_USAGE

        # A produce retry redelivers the same spans in a later poll.
        await job._process_poll(records)
        assert _usage(client, project_id) == _EXPECTED_USAGE
    finally:
        _delete_project(client, (*_USAGE_SOURCES, "usage_minute"), project_id)
