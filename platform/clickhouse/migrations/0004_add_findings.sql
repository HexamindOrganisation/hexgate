-- init/ only executes on an empty volume, so every environment that already has
-- the hexgate_audit database gets this by replaying this directory: `make
-- platform-migrate STAGE=<stage>` for a deploy stack, `make clickhouse-migrate`
-- for the local dev compose.
--
-- ORDERING: apply this BEFORE deploying the build that ships findings — the one
-- whose verify_all tuple names audit_finding and whose detector job writes all
-- four tables. Skipping it on that build is not a degraded findings page, it is
-- a refused boot:
--   * startup — verify_all sees audit_finding as every column missing and the
--     api exits; platform-up has already recreated its container, so it
--     crash-loops until this lands.
--   * the detector job, had startup not caught it — every tick's insert names a
--     table ClickHouse does not have, so the tick fails, the watermark never
--     advances, and the job retries the same window forever.
-- The four CREATEs are additive (an older build never references these tables,
-- and nothing on the ingest path touches them), so they can be applied
-- arbitrarily early. Idempotent (IF NOT EXISTS).
--
-- Keep these statements identical to the ones in init/schema.sql so a
-- hand-migrated volume and a freshly-initialized one end up with the same
-- tables. tests/features/findings/test_finding_schema.py asserts that without a
-- database and tests/core/test_migrations.py asserts it against a real one,
-- which is why the four names are also in its AUDIT_TABLES tuple.

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
