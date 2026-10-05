"""usage_minute fed through the real enricher against a local ClickHouse.

F1 end to end: a view sums every inserted copy, so the enricher's cross-poll
dedup is what keeps a redelivered span from being counted twice. Opt-in via
`pytest -m integration`; needs migration 0005 on the local volume
(`make clickhouse-migrate`).
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
    decision_attrs,
    make_request_bytes,
    make_span,
    run_attrs,
    usage_attrs,
)

_SOURCE_TABLES = ("policy_decision", "llm_invocation", "agent_run")
_ROLLUP_TABLE = "usage_minute"
# invocations, tool_calls, denials, llm_calls, input_tokens, output_tokens
_EXPECTED = (1, 2, 1, 1, 100, 50)


def _usage(client, project_id: str) -> tuple:
    return tuple(
        client.query(
            "SELECT sum(invocations), sum(tool_calls), sum(denials), "
            "sum(llm_calls), sum(input_tokens), sum(output_tokens) "
            f"FROM {_ROLLUP_TABLE} WHERE project_id = {{pid:String}}",
            parameters={"pid": project_id},
        ).result_rows[0]
    )


@pytest.mark.integration
async def test_a_redelivered_poll_is_not_summed_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hexgate_api.core.clickhouse import get_clickhouse

    client = get_clickhouse()
    project_id = f"test_proj_{uuid.uuid4().hex[:8]}"

    async def _stub_resolve(pairs):
        return {pair: "ver_int" for pair in pairs}

    monkeypatch.setattr(
        "hexgate_api.jobs.enricher.consumer.resolve_versions", _stub_resolve
    )
    calls: list[str] = []
    job = EnricherJob(
        Settings(enricher_insert_max_backoff_s=0.01),
        clickhouse_client=client,
        consumer=FakeConsumer(calls),
        producer=FakeProducer(calls),
    )
    records = [
        FakeRecord(
            key=project_id.encode(),
            value=make_request_bytes(
                [
                    (semconv.SCOPE_RUNS, [make_span(run_attrs())]),
                    (
                        semconv.SCOPE_AUDIT,
                        [
                            make_span(decision_attrs()),
                            make_span(decision_attrs(**{semconv.OUTCOME: "deny"})),
                            make_span(
                                decision_attrs(**{semconv.OUTCOME: "needs_approval"})
                            ),
                            # Egress is decided outside the guard runner: dropped.
                            make_span(
                                decision_attrs(
                                    **{semconv.TOOL_NAME: "net.http_request"}
                                )
                            ),
                        ],
                    ),
                    (semconv.SCOPE_USAGE, [make_span(usage_attrs())]),
                ]
            ),
        )
    ]
    try:
        await job._process_poll(records)
        assert _usage(client, project_id) == _EXPECTED

        # A produce retry redelivers the same spans in a later poll.
        await job._process_poll(records)
        assert _usage(client, project_id) == _EXPECTED
    finally:
        for table in (*_SOURCE_TABLES, _ROLLUP_TABLE):
            client.command(
                f"ALTER TABLE {table} DELETE WHERE project_id = {{pid:String}}",
                parameters={"pid": project_id},
            )
