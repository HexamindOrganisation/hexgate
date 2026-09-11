-- init/ only executes on an empty volume, so every environment that already
-- has the hexgate_audit database gets this from `make platform-migrate
-- STAGE=<stage>` (deploy stacks — replays this directory) or by pasting the
-- statement into `make clickhouse-cli` (the local dev compose).
--
-- ORDERING: apply this BEFORE deploying the build that stores hexgate.messages
-- (the one whose KNOWN_SCOPES holds the scope and whose verify_all tuple names
-- this table, in both main.py and the enricher). Skipping it on that build is
-- not a degraded message path, it is a refused boot:
--   * startup — verify_all sees the table as every column missing and both
--     the api and the enricher exit; platform-up has already recreated their
--     containers, so they crash-loop until this lands.
--   * ingest, had startup not caught it — the messages insert names the
--     table, ClickHouse rejects the batch, and the enricher retries the whole
--     poll until acked, holding the partition's decisions, usage and bans with
--     it (design: an audit log must not silently lose acknowledged rows).
-- The CREATE is additive — an older build never references the table — so it
-- can be applied arbitrarily early. Idempotent (IF NOT EXISTS).
--
-- Keep this statement byte-identical to the one in init/schema.sql so a
-- hand-migrated volume and a freshly-initialized one end up with the same
-- table, which is what _LLM_MESSAGE_COLUMNS ("order matches schema.sql") will
-- rely on.

CREATE TABLE IF NOT EXISTS hexgate_audit.llm_message
(
    event_id            UUID,
    occurred_at         DateTime64(3, 'UTC'),
    received_at         DateTime64(3, 'UTC') DEFAULT now64(3),
    project_id          LowCardinality(String),
    agent_name          LowCardinality(String),
    agent_version_id    LowCardinality(String) DEFAULT '',
    session_id          String DEFAULT '',
    user_id             LowCardinality(String) DEFAULT '',

    model               LowCardinality(String),
    turn_key            String,
    message_seq         UInt32,
    resynced            UInt8 DEFAULT 0,
    truncated           UInt8 DEFAULT 0,
    input_messages      String COMMENT 'gen_ai.input.messages — only the messages new to this call, tool results included; capped 256 KiB' CODEC(ZSTD(3)),
    output_messages     String COMMENT 'gen_ai.output.messages — this call''s completion; capped 8 KiB' CODEC(ZSTD(3)),
    system_instructions String DEFAULT '' COMMENT 'gen_ai.system_instructions — first row of each turn_key only; capped 8 KiB' CODEC(ZSTD(3)),

    run_id              UUID DEFAULT toUUID('00000000-0000-0000-0000-000000000000') COMMENT 'RunFacts.id of the run this exchange belongs to; zero when outside a run scope or from an SDK that does not yet send it'
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(received_at)
ORDER BY (project_id, session_id, occurred_at, message_seq, event_id)
TTL toDateTime(received_at) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;
