-- init/ only executes on an empty volume, so every environment that already
-- has the hexgate_audit database gets this from `make platform-migrate
-- STAGE=<stage>` (deploy stacks — replays this directory) or `make
-- clickhouse-migrate` (the local dev compose).
--
-- ORDERING: apply this BEFORE deploying the enricher build whose KNOWN_SCOPES
-- holds hexgate.runs (the one whose verify_all tuple names this table).
-- Skipping it on that build is a refused boot: verify_all sees the table as
-- every column missing and the enricher exits; platform-up has already
-- recreated its container, so it crash-loops until this lands.
-- The CREATE is additive — an older build never references the table — so it
-- can be applied arbitrarily early. Idempotent (IF NOT EXISTS).
--
-- Keep this statement byte-identical to the one in init/schema.sql so a
-- hand-migrated volume and a freshly-initialized one end up with the same
-- table, which is what _AGENT_RUN_COLUMNS ("order matches schema.sql") relies
-- on.

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
