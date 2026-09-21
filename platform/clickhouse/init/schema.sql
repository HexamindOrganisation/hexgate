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


-- ---------------------------------------------------------------------------
-- Findings (advanced anomaly detection, phase 1) — four tables written by the
-- detector job, read by the findings endpoint. Nothing on the ingest path
-- touches them: the detectors read policy_decision rows already stored, so the
-- SDK wire format, the Collector and the enricher are unaffected.
-- ---------------------------------------------------------------------------


-- One row per finding, updated in place rather than appended to: a long run is
-- re-scored every tick, and each re-score writes the SAME finding_id with a
-- newer detected_at, so the later row wins and the run stays one finding.
CREATE TABLE IF NOT EXISTS hexgate_audit.audit_finding
(
    finding_id          UUID COMMENT 'uuid5(project_id, kind, subject_id, bucket) — deterministic, and the dedup identity',
    detected_at         DateTime64(3, 'UTC') DEFAULT now64(3) COMMENT 'ReplacingMergeTree version column — the later row wins',
    project_id          LowCardinality(String),

    kind                LowCardinality(String) COMMENT 'first_seen | run_shape | repetition | deny_burst | conformance',
    severity            LowCardinality(String) COMMENT 'low | medium | high',
    subject             LowCardinality(String) COMMENT 'user | agent | run — what the finding is about, and what a mute targets',
    subject_id          String,

    -- Denormalised join keys, defaulted rather than nullable (llm_invocation's
    -- convention for run_id).
    agent_name          LowCardinality(String) DEFAULT '',
    run_id              UUID DEFAULT toUUID('00000000-0000-0000-0000-000000000000'),
    session_id          String DEFAULT '',

    -- The FINDING's span, not the scoring tick's. first_seen carries the
    -- earliest value ever written for this finding_id: a tick only reads
    -- decisions past the watermark, so recomputing the minimum there would walk
    -- a finding's start time forward on every tick with nothing erroring.
    first_seen          DateTime64(3, 'UTC'),
    last_seen           DateTime64(3, 'UTC'),

    summary             String COMMENT 'one sentence, rendered server-side — the UI never assembles it',
    evidence            String DEFAULT '' COMMENT 'per-kind JSON payload' CODEC(ZSTD(3)),
    suggested_control   String DEFAULT '' COMMENT 'JSON policy fragment that would stop a repeat — empty for kinds that have none' CODEC(ZSTD(3))
)
ENGINE = ReplacingMergeTree(detected_at)
PARTITION BY toYYYYMM(detected_at)
-- IDENTITY ONLY, and every column in it immutable. ReplacingMergeTree collapses
-- rows whose WHOLE sorting key is equal, and severity and last_seen both move
-- as a run grows — either in the key would store one long run as one row per
-- tick at escalating severities, with the uuid5 bucket doing nothing. Reads pay
-- for this with FINAL and a read-time sort, affordable because the table holds
-- ~5 rows per 1000 runs. Dedup is also per-partition, so a finding re-scored
-- across a month boundary keeps two rows until TTL; FINAL hides that too.
ORDER BY (project_id, finding_id)
TTL toDateTime(detected_at) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;


-- When a scope first exhibited a feature value (a tool name, a model, a
-- role/tool pair) — how the first_seen detector tells novel from uncommon.
CREATE TABLE IF NOT EXISTS hexgate_audit.feature_first_seen
(
    project_id          LowCardinality(String),
    scope_kind          LowCardinality(String) COMMENT 'project | user | agent — whose first sighting this is',
    scope_key           String COMMENT 'the id within scope_kind — empty for the project scope',
    feature_kind        LowCardinality(String) COMMENT 'which feature space the value lives in, e.g. tool | model | role_tool',
    feature_value       String,

    -- AggregatingMergeTree, not Replacing: a ReplacingMergeTree keeps the
    -- NEWEST row, exactly backwards here — the second sighting would overwrite
    -- the first and nothing would ever read as novel again. min() over a
    -- SimpleAggregateFunction keeps the earliest across merges instead, so
    -- re-processing a window cannot move a first sighting later.
    first_at            SimpleAggregateFunction(min, DateTime64(3, 'UTC')),
    seen_count          SimpleAggregateFunction(sum, UInt64) COMMENT 'read by the novelty floor — a scope with almost no history makes everything look novel'
)
ENGINE = AggregatingMergeTree
-- No PARTITION BY and no TTL, both deliberate. Partitioning on first_at would
-- confine the min() to one partition, so a later re-sighting would sit beside
-- the original instead of collapsing into it; a TTL would expire the OLDEST
-- rows, the only ones this table exists to keep, and every long-established
-- feature would read as novel the day it aged out.
--
-- RETENTION, unresolved and left here on purpose. This is the only table in
-- this file with no TTL that will hold a user identifier (scope_kind='user'),
-- so once the detector writes those rows they outlive by design the
-- policy_decision rows they were derived from, which drop at 180 days. The
-- obvious fix does NOT work: a `last_at` column with
-- `TTL toDateTime(last_at) + INTERVAL 180 DAY` was measured against a real
-- server and silently corrupts the table — TTL is evaluated per part, so an
-- ancient part is deleted before it merges with a recent one and the surviving
-- row reports a first sighting years too late (alice's first_at moved 2024 ->
-- 2026 and her seen_count fell from 7 to 2). Whoever ships the user scope
-- settles this: hash scope_key, keep the scope project/agent-only, or expire by
-- deleting whole scope_keys on a schedule rather than by TTL.
ORDER BY (project_id, scope_kind, scope_key, feature_kind, feature_value)
SETTINGS index_granularity = 8192;


-- Per-agent, per-feature distribution summary, rebuilt hourly by a full 14-day
-- scan: median, MAD and p99 need the whole distribution, so there is nothing to
-- add incrementally and the rebuild is deliberately not watermarked.
CREATE TABLE IF NOT EXISTS hexgate_audit.agent_baseline
(
    project_id          LowCardinality(String),
    agent_name          LowCardinality(String),
    feature             LowCardinality(String) COMMENT 'the run_* accumulator summarised, e.g. tool_calls | llm_calls | total_tokens | elapsed_ms',
    computed_at         DateTime64(3, 'UTC') DEFAULT now64(3) COMMENT 'ReplacingMergeTree version column — the newest rebuild wins',

    -- Robust statistics on log1p(x), not mean and stddev, so one pathological
    -- run cannot widen the band that would have caught it.
    median_log1p        Float64,
    mad_log1p           Float64,
    -- Raw-scale bounds: the fallback when mad_log1p is 0 (an agent that always
    -- does exactly 3 steps), where a robust z is infinite and pages everyone.
    observed_min        Float64,
    observed_max        Float64,
    p99                 Float64 COMMENT 'the one owner of the declaration ceiling — the run.tool_calls <= N bound IS this number, not a second one',

    -- Cold start: a baseline stays learning until it has both enough runs and
    -- enough wall-clock span. The span half is not decoration — without it 50
    -- runs generated in five minutes become "normal", which is what a seeding
    -- script produces.
    run_count           UInt64,
    span_hours          Float64,
    state               LowCardinality(String) COMMENT 'learning | ready — a learning baseline builds and emits nothing'
)
ENGINE = ReplacingMergeTree(computed_at)
-- No PARTITION BY: one row per agent-feature, and partitioning on computed_at
-- would put each hourly rebuild in a partition dedup never reaches, growing the
-- table by a full copy every hour.
ORDER BY (project_id, agent_name, feature)
SETTINGS index_granularity = 8192;


-- How far each detector has consumed, per project. On received_at, never
-- occurred_at: occurred_at is client-supplied, so a row arriving with a skewed
-- clock would land behind the watermark and never be scored.
CREATE TABLE IF NOT EXISTS hexgate_audit.detector_watermark
(
    project_id          LowCardinality(String),
    detector            LowCardinality(String),
    watermark           DateTime64(3, 'UTC') COMMENT 'max received_at processed — the next tick reads strictly after it',
    updated_at          DateTime64(3, 'UTC') DEFAULT now64(3) COMMENT 'ReplacingMergeTree version column'
)
ENGINE = ReplacingMergeTree(updated_at)
-- No PARTITION BY, same reason as agent_baseline: a handful of rows that must
-- collapse to one per (project, detector) forever.
ORDER BY (project_id, detector)
SETTINGS index_granularity = 8192;
