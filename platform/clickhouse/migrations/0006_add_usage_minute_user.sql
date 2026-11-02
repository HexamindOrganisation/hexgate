-- init/ only executes on an empty volume, so every environment that already
-- has the hexgate_audit database gets this from `make platform-migrate
-- STAGE=<stage>` (deploy stacks — replays this directory) or `make
-- clickhouse-migrate` (the local dev compose).
--
-- ORDERING: apply this only with the writers stopped (api + enricher), as the
-- standard upgrade does: platform-stop-writers, platform-migrate, then the
-- one-time backfill BEFORE platform-up: `make platform-backfill STAGE=<stage>
-- FILE=0006_usage_minute_user` (platform/DEPLOY.md § 6). A row written between
-- this CREATE and the backfill is counted by a view and by the backfill, for
-- good. Requires 0005's enricher (cross-poll dedup): never run beside an older one.
-- The CREATEs are additive — older code never names these objects — and
-- idempotent (IF NOT EXISTS).
--
-- Keep every statement byte-identical to the one in init/schema.sql;
-- test_a_migrated_clickhouse_matches_a_fresh_one compares the definitions.

-- Per-minute usage per end user, project-wide: the source of user_usage.*
-- windows. A mechanical copy of usage_minute (0005) except for the key column
-- and the anonymous filter; tests/core/test_usage_minute_sql.py pins that. Fed
-- only by the three views below; nothing inserts here directly except the
-- one-time backfill (clickhouse/backfills/0006_usage_minute_user.sql). Correct
-- only while the enricher dedups by event_id across polls: a view sums every
-- inserted copy. Bucketed on server-stamped received_at.
-- user_id is plain String: a B2C project passes LowCardinality's sweet spot.
-- No '' row: anonymous traffic is not one user (WHERE user_id != '' below).
-- Personal data: kept 35 days (TTL); erase with ALTER TABLE ... DELETE WHERE
-- user_id = ..., as on the raw tables.
CREATE TABLE IF NOT EXISTS hexgate_audit.usage_minute_user
(
    project_id          LowCardinality(String),
    user_id             String,
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
ORDER BY (project_id, user_id, minute)
-- Longest window (30d) plus slack.
TTL minute + INTERVAL 35 DAY
SETTINGS index_granularity = 8192;

CREATE MATERIALIZED VIEW IF NOT EXISTS hexgate_audit.usage_minute_user_from_runs
TO hexgate_audit.usage_minute_user
AS SELECT
    project_id,
    user_id,
    toStartOfMinute(received_at) AS minute,
    count() AS invocations,
    0 AS tool_calls,
    0 AS denials,
    0 AS llm_calls,
    0 AS input_tokens,
    0 AS output_tokens
FROM hexgate_audit.agent_run
WHERE user_id != ''
GROUP BY project_id, user_id, minute;

-- The same decision filter as usage_minute_from_decisions: what run.tool_calls
-- and run.denials count. Keep the two identical.
CREATE MATERIALIZED VIEW IF NOT EXISTS hexgate_audit.usage_minute_user_from_decisions
TO hexgate_audit.usage_minute_user
AS SELECT
    project_id,
    user_id,
    toStartOfMinute(received_at) AS minute,
    0 AS invocations,
    countIf(outcome IN ('allow', 'needs_approval')) AS tool_calls,
    countIf(outcome = 'deny') AS denials,
    0 AS llm_calls,
    0 AS input_tokens,
    0 AS output_tokens
FROM hexgate_audit.policy_decision
WHERE user_id != ''
  AND tool_name != 'agent.run'
  AND NOT startsWith(tool_name, 'agent.handoff:')
  AND NOT startsWith(tool_name, 'net.')
GROUP BY project_id, user_id, minute;

CREATE MATERIALIZED VIEW IF NOT EXISTS hexgate_audit.usage_minute_user_from_llm
TO hexgate_audit.usage_minute_user
AS SELECT
    project_id,
    user_id,
    toStartOfMinute(received_at) AS minute,
    0 AS invocations,
    0 AS tool_calls,
    0 AS denials,
    count() AS llm_calls,
    sum(input_tokens) AS input_tokens,
    sum(output_tokens) AS output_tokens
FROM hexgate_audit.llm_invocation
WHERE user_id != ''
GROUP BY project_id, user_id, minute;
