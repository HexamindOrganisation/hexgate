-- Audit log for policy decisions emitted by PolicyEnforcer.decide().
--
-- The first nine columns are the shared "envelope" that every future
-- audit event table (tool_invocation, tool_completion, ...) is expected
-- to carry, in the same order, with the same types. This keeps
-- cross-event correlation (UNIONs, JOINs, session_timeline views)
-- trivial as new event types land.
--
-- HOW THIS FILE GETS APPLIED — and why that's a POC scaffold:
-- The Docker image runs everything under /docker-entrypoint-initdb.d
-- exactly once, on first container start with an empty data volume.
-- That means:
--   * Editing this file after first init has NO effect on a running
--     environment — the change is silently ignored.
--   * Dropping a 02_*.sql neighbor does NOT migrate anyone whose
--     volume already exists. The numeric prefix sets file order on
--     first init, not "next migration to apply."
--   * To apply schema changes to an existing local install, either run
--     `make clickhouse-reset` (wipes data) or apply the SQL by hand
--     via `make clickhouse-cli`.
-- This works for PR 1 because there's exactly one schema to apply.
-- The first time a second schema change is needed, this mechanism
-- should be replaced with a real migration runner (Python script
-- tracking applied versions in a _schema_migrations table, or
-- golang-migrate). Do not extend this directory with 02_*.sql.

CREATE DATABASE IF NOT EXISTS fortify_audit;

CREATE TABLE IF NOT EXISTS fortify_audit.policy_decision
(
    -- Envelope (shared across all future event tables)
    event_id            UUID,
    occurred_at         DateTime64(3, 'UTC'),
    received_at         DateTime64(3, 'UTC') DEFAULT now64(3),
    project_id          LowCardinality(String),
    agent_name          LowCardinality(String),
    agent_version       LowCardinality(String) DEFAULT '',
    policy_content_hash LowCardinality(String) DEFAULT '',
    session_id          String DEFAULT '',
    user_id             LowCardinality(String) DEFAULT '',

    -- Decision-specific
    tool_name           LowCardinality(String),
    role                LowCardinality(String) DEFAULT '',
    outcome             Enum8('allow' = 1, 'deny' = 2, 'needs_approval' = 3),
    error_type          LowCardinality(String) DEFAULT '',
    reason              String,
    violations          Array(String),
    hint                String CODEC(ZSTD(3)),
    arguments           String COMMENT 'SDK-truncated JSON snapshot; may be lossy' CODEC(ZSTD(3))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(occurred_at)
ORDER BY (project_id, occurred_at, agent_name, outcome)
TTL toDateTime(occurred_at) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;
