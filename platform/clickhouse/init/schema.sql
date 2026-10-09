-- Audit log for PolicyEnforcer decisions.
-- The first eight columns are the envelope shared with future event
-- tables (tool_invocation, ...) — same names, types, and order.
-- This init dir runs once on an empty volume; edits afterward are
-- ignored. Don't add more files here — use a real migration runner
-- instead. Until there is one: every edit below also needs a
-- hand-applied counterpart in ../migrations/ for existing volumes —
-- unless no ALTER can restate pre-existing rows truthfully (the role
-- set), in which case the volume is recreated and no migration ships.

CREATE DATABASE IF NOT EXISTS hexgate_audit;

CREATE TABLE IF NOT EXISTS hexgate_audit.policy_decision
(
    -- Envelope (shared across all future event tables)
    event_id            UUID,
    occurred_at         DateTime64(3, 'UTC'),
    received_at         DateTime64(3, 'UTC') DEFAULT now64(3),
    project_id          LowCardinality(String),
    agent_name          LowCardinality(String),
    agent_version_id    LowCardinality(String) DEFAULT '',
    session_id          String DEFAULT '',
    user_id             LowCardinality(String) DEFAULT '',

    -- Decision-specific
    tool_name           LowCardinality(String),
    outcome             Enum8('allow' = 1, 'deny' = 2, 'needs_approval' = 3),
    error_type          LowCardinality(String) DEFAULT '',
    reason              String,
    violations          Array(String),
    hint                String CODEC(ZSTD(3)),
    arguments           String COMMENT 'SDK-truncated JSON snapshot; may be lossy' CODEC(ZSTD(3)),
    attributes          String COMMENT 'Caller ABAC bag (ctx.*); advisory + client-assertable; SDK-redacted and truncated' CODEC(ZSTD(3)),
    -- The caller's roles are a set, stored only as a set — there is no legacy
    -- scalar `role` column. An SDK predating multi-role sends one; it is folded
    -- into user_roles at ingest (audit/service.py), so every row here is the
    -- same shape whatever wrote it. No DEFAULT: these columns have existed
    -- since the first CREATE, so nothing needs a read-time rescue.
    user_roles          Array(LowCardinality(String)) COMMENT 'Distinct roles evaluated for this call, caller order; advisory + client-assertable',
    deciding_role       LowCardinality(String) DEFAULT '' COMMENT 'Role whose policy granted/gated the call; empty when every role denied',

    -- Run attribution (advisory + client-assertable, same tier as user_id /
    -- user_roles). Zero UUID / zero counters means "not attributed to a run
    -- scope by the SDK that sent this row" — a truthful default until the
    -- SDK starts stamping the run_ns namespace read once per decision
    -- (hexgate/security/enforcer.py). See plans/run-state/run-state-schema.md §7.
    run_id              UUID     DEFAULT toUUID('00000000-0000-0000-0000-000000000000') COMMENT 'RunFacts.id; zero when the decision was made outside a run scope or by an SDK that does not yet send it',
    run_tool_calls      UInt32   DEFAULT 0 COMMENT 'run.tool_calls at decision time',
    run_llm_calls       UInt32   DEFAULT 0 COMMENT 'run.llm_calls at decision time',
    run_denials         UInt32   DEFAULT 0 COMMENT 'run.denials at decision time',
    run_total_tokens    UInt32   DEFAULT 0 COMMENT 'run.total_tokens at decision time',
    run_elapsed_ms      UInt32   DEFAULT 0 COMMENT 'run.elapsed_seconds * 1000 at decision time'
)
-- ReplacingMergeTree: SDK retries (same event_id) collapse on background
-- merges — eventual dedup; exact counts use FINAL or count(DISTINCT event_id).
ENGINE = ReplacingMergeTree(received_at)
-- Partition + TTL anchor on server-stamped received_at, not the
-- client-supplied occurred_at (clock skew would break retention).
PARTITION BY toYYYYMM(received_at)
ORDER BY (project_id, agent_name, outcome, occurred_at, event_id)
TTL toDateTime(received_at) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;


-- Kill-switch ban enforcements — one row per execution refused at the invoke gate.
-- Sibling of policy_decision sharing the envelope; separate table because a ban
-- has no tool/outcome and its per-attempt volume would swamp the decision feed.
CREATE TABLE IF NOT EXISTS hexgate_audit.ban_enforcement
(
    -- Envelope (shared with policy_decision — same names, types, order)
    event_id            UUID,
    occurred_at         DateTime64(3, 'UTC'),
    received_at         DateTime64(3, 'UTC') DEFAULT now64(3),
    project_id          LowCardinality(String),
    agent_name          LowCardinality(String),
    agent_version_id    LowCardinality(String) DEFAULT '',
    session_id          String DEFAULT '',
    user_id             LowCardinality(String) DEFAULT '',

    -- Ban-specific
    ban_type            Enum8('agent' = 1, 'user' = 2),
    ban_id              LowCardinality(String),
    reason              String
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(received_at)
ORDER BY (project_id, occurred_at, event_id)
TTL toDateTime(received_at) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;


CREATE TABLE IF NOT EXISTS hexgate_audit.llm_invocation
(
    -- Envelope (shared across all future event tables)
    event_id            UUID,
    occurred_at         DateTime64(3, 'UTC'),
    received_at         DateTime64(3, 'UTC') DEFAULT now64(3),
    project_id          LowCardinality(String),
    agent_name          LowCardinality(String),
    agent_version_id    LowCardinality(String) DEFAULT '',
    session_id          String DEFAULT '',
    user_id             LowCardinality(String) DEFAULT '',

    -- LLM-invocation-specific
    model               LowCardinality(String),
    input_tokens        UInt32,
    output_tokens       UInt32,
    latency_ms          UInt32 DEFAULT 0,
    status              LowCardinality(String) DEFAULT 'success',
    error_code          LowCardinality(String) DEFAULT '',

    run_id              UUID DEFAULT toUUID('00000000-0000-0000-0000-000000000000') COMMENT 'RunFacts.id of the run this LLM call belongs to; zero when outside a run scope or from an SDK that does not yet send it'
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(received_at)
ORDER BY (project_id, user_id, agent_name, model, occurred_at, event_id)
TTL toDateTime(received_at) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;


-- LLM prompt/completion content — one row per model call, scope hexgate.messages.
-- Sibling of llm_invocation sharing the envelope; a separate table because the
-- content is large, opt-in (nothing emits hexgate.messages yet, and capture
-- stays off until an emitter ships) and read by session, not aggregated by
-- user/model like token usage.
CREATE TABLE IF NOT EXISTS hexgate_audit.llm_message
(
    -- Envelope (shared with the other event tables — same names, types, order)
    event_id            UUID,
    occurred_at         DateTime64(3, 'UTC'),
    received_at         DateTime64(3, 'UTC') DEFAULT now64(3),
    project_id          LowCardinality(String),
    agent_name          LowCardinality(String),
    agent_version_id    LowCardinality(String) DEFAULT '',
    session_id          String DEFAULT '',
    user_id             LowCardinality(String) DEFAULT '',

    -- Message-specific
    model               LowCardinality(String),
    -- Which framework message list this row extends: one per run / sub-agent /
    -- handoff. message_seq counts rows within it, so a reader seeing 0, 1, 3
    -- knows a row is missing instead of trusting a shorter transcript.
    turn_key            String,
    message_seq         UInt32,
    -- 1 when this row restates the whole list (the framework trimmed or
    -- summarised it) rather than extending it — read it as history, not delta.
    resynced            UInt8 DEFAULT 0,
    -- 1 when any content column below was cut to its byte cap (head+tail).
    truncated           UInt8 DEFAULT 0,
    -- Official gen_ai.* shapes as JSON; SDK-redacted and capped, may be lossy.
    input_messages      String COMMENT 'gen_ai.input.messages — only the messages new to this call, tool results included; capped 256 KiB' CODEC(ZSTD(3)),
    output_messages     String COMMENT 'gen_ai.output.messages — this call''s completion; capped 8 KiB' CODEC(ZSTD(3)),
    system_instructions String DEFAULT '' COMMENT 'gen_ai.system_instructions — first row of each turn_key only; capped 8 KiB' CODEC(ZSTD(3)),

    run_id              UUID DEFAULT toUUID('00000000-0000-0000-0000-000000000000') COMMENT 'RunFacts.id of the run this exchange belongs to; zero when outside a run scope or from an SDK that does not yet send it'
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(received_at)
-- Session-first, then time. message_seq only counts within one turn_key and
-- restarts at 0 for a sub-agent's or handoff's list, so wall-clock occurred_at
-- is the only thing that orders rows across the several lists of one session —
-- hence third, ahead of message_seq, which completes the (occurred_at,
-- message_seq) order the read endpoint returns rows in. turn_key stays a plain
-- column: once occurred_at precedes it a list's rows are no longer adjacent
-- anyway, so in the key it would only break ties event_id already resolves.
-- event_id last keeps dedup (the sort key IS the dedup key here) to SDK
-- retries of the same event.
ORDER BY (project_id, session_id, occurred_at, message_seq, event_id)
TTL toDateTime(received_at) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;

-- One row per admitted agent run — scope hexgate.runs, emitted on run_scope entry
-- (after the ban check and admission). The only record of a run that made no tool
-- or model call; the invocation count agent_usage.* is built from.
CREATE TABLE IF NOT EXISTS hexgate_audit.agent_run
(
    -- Envelope (shared with the other event tables — same names, types, order)
    event_id            UUID,
    occurred_at         DateTime64(3, 'UTC'),
    received_at         DateTime64(3, 'UTC') DEFAULT now64(3),
    project_id          LowCardinality(String),
    agent_name          LowCardinality(String),
    agent_version_id    LowCardinality(String) DEFAULT '',
    session_id          String DEFAULT '',
    user_id             LowCardinality(String) DEFAULT '',

    -- Run-specific. No zero-UUID default, unlike the sibling tables: a run-start
    -- span without a run id is rejected to the DLQ, never stored.
    run_id              UUID COMMENT 'RunFacts.id — joins to policy_decision / llm_invocation / llm_message rows of the same run'
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(received_at)
-- Per agent over time: the read is "runs of this agent in a window". event_id
-- last keeps dedup (the sort key IS the dedup key) to SDK retries of one event.
ORDER BY (project_id, agent_name, occurred_at, event_id)
TTL toDateTime(received_at) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;

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
