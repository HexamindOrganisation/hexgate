-- init/ only executes on an empty volume, so every environment that already
-- has the hexgate_audit database gets this from `make platform-migrate
-- STAGE=<stage>` (deploy stacks — replays this directory) or `make
-- clickhouse-migrate` (the local dev compose).
--
-- ORDERING: apply this only while no enricher older than this build is running.
-- The standard upgrade does exactly that: platform-stop-writers stops the old
-- enricher before platform-migrate, and platform-up starts the new one. Created
-- beside an old enricher, which dedups only within a poll, every retried span is
-- summed into usage_minute twice, permanently. Then run the one-time backfill
-- BEFORE platform-up: `make platform-backfill STAGE=<stage>
-- FILE=0005_usage_minute` (platform/DEPLOY.md § 6).
-- The CREATEs are additive — older code never names these objects — and
-- idempotent (IF NOT EXISTS).
--
-- Keep every statement byte-identical to the one in init/schema.sql;
-- test_a_migrated_clickhouse_matches_a_fresh_one compares the definitions.

-- Per-minute usage per agent, the source of agent_usage.* windows. Fed only by
-- the three views below; nothing inserts here directly except the one-time
-- backfill (clickhouse/backfills/0005_usage_minute.sql). Correct only while the
-- enricher dedups by event_id across polls: a view sums every inserted copy.
-- Bucketed on server-stamped received_at, never the SDK-asserted occurred_at.
-- No user_id: one row per agent per minute, whatever the number of users.
CREATE TABLE IF NOT EXISTS hexgate_audit.usage_minute
(
    project_id          LowCardinality(String),
    agent_name          LowCardinality(String),
    minute              DateTime('UTC'),
    invocations         UInt64,
    tool_calls          UInt64,
    denials             UInt64,
    llm_calls           UInt64,
    input_tokens        UInt64,
    output_tokens       UInt64
)
-- Rows sharing a key are summed on merge, eventually: always read with
-- sum() ... GROUP BY, never assume one row per key.
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY (project_id, agent_name, minute)
-- Longest window (30d) plus slack.
TTL minute + INTERVAL 35 DAY
SETTINGS index_granularity = 8192;

CREATE MATERIALIZED VIEW IF NOT EXISTS hexgate_audit.usage_minute_from_runs
TO hexgate_audit.usage_minute
AS SELECT
    project_id,
    agent_name,
    toStartOfMinute(received_at) AS minute,
    count() AS invocations,
    0 AS tool_calls,
    0 AS denials,
    0 AS llm_calls,
    0 AS input_tokens,
    0 AS output_tokens
FROM hexgate_audit.agent_run
GROUP BY project_id, agent_name, minute;

-- Counts exactly what the SDK's run.tool_calls / run.denials count: decisions
-- made through the guard runner. Admission (agent.run), handoff reach
-- (agent.handoff:) and egress (net.*) are decided outside it and are dropped;
-- agent-as-tool reach (agent.tool:) and skill keys go through it and are kept.
-- needs_approval counts as a tool call like allow: an approved call ran, and a
-- refused one over-counts, the safe way round for a quota.
CREATE MATERIALIZED VIEW IF NOT EXISTS hexgate_audit.usage_minute_from_decisions
TO hexgate_audit.usage_minute
AS SELECT
    project_id,
    agent_name,
    toStartOfMinute(received_at) AS minute,
    0 AS invocations,
    countIf(outcome IN ('allow', 'needs_approval')) AS tool_calls,
    countIf(outcome = 'deny') AS denials,
    0 AS llm_calls,
    0 AS input_tokens,
    0 AS output_tokens
FROM hexgate_audit.policy_decision
WHERE tool_name != 'agent.run'
  AND NOT startsWith(tool_name, 'agent.handoff:')
  AND NOT startsWith(tool_name, 'net.')
GROUP BY project_id, agent_name, minute;

CREATE MATERIALIZED VIEW IF NOT EXISTS hexgate_audit.usage_minute_from_llm
TO hexgate_audit.usage_minute
AS SELECT
    project_id,
    agent_name,
    toStartOfMinute(received_at) AS minute,
    0 AS invocations,
    0 AS tool_calls,
    0 AS denials,
    count() AS llm_calls,
    sum(input_tokens) AS input_tokens,
    sum(output_tokens) AS output_tokens
FROM hexgate_audit.llm_invocation
GROUP BY project_id, agent_name, minute;
