-- One-time fill of usage_minute from the last 30 days of raw rows, so the first
-- 30d reads after migration 0005 are not zero. Run once per stage, after 0005
-- and while api + enricher are stopped (platform/DEPLOY.md § 6), so no row is
-- both backfilled and seen by a view: `make platform-backfill STAGE=<stage>
-- FILE=0005_usage_minute`.
--
-- Guarded: inserts nothing unless usage_minute is empty, so a second run is a
-- no-op (the scalar subquery is evaluated before the insert writes).
-- The same file rebuilds the rollup: stop the writers, TRUNCATE
-- hexgate_audit.usage_minute, run this, start the writers. FINAL collapses
-- duplicates the raw tables have not merged yet.
--
-- The SELECT filters must stay identical to the views in init/schema.sql;
-- test_usage_minute_backfill_matches_the_views pins that.
INSERT INTO hexgate_audit.usage_minute
SELECT project_id, agent_name, minute, invocations, tool_calls, denials,
       llm_calls, input_tokens, output_tokens
FROM
(
    SELECT project_id, agent_name, toStartOfMinute(received_at) AS minute,
           count() AS invocations, 0 AS tool_calls, 0 AS denials,
           0 AS llm_calls, 0 AS input_tokens, 0 AS output_tokens
    FROM hexgate_audit.agent_run FINAL
    WHERE received_at >= now() - INTERVAL 30 DAY
    GROUP BY project_id, agent_name, minute

    UNION ALL

    SELECT project_id, agent_name, toStartOfMinute(received_at) AS minute,
           0 AS invocations,
           countIf(outcome IN ('allow', 'needs_approval')) AS tool_calls,
           countIf(outcome = 'deny') AS denials,
           0 AS llm_calls, 0 AS input_tokens, 0 AS output_tokens
    FROM hexgate_audit.policy_decision FINAL
    WHERE received_at >= now() - INTERVAL 30 DAY
      AND tool_name != 'agent.run'
      AND NOT startsWith(tool_name, 'agent.handoff:')
      AND NOT startsWith(tool_name, 'net.')
    GROUP BY project_id, agent_name, minute

    UNION ALL

    SELECT project_id, agent_name, toStartOfMinute(received_at) AS minute,
           0 AS invocations, 0 AS tool_calls, 0 AS denials,
           count() AS llm_calls, sum(input_tokens) AS input_tokens,
           sum(output_tokens) AS output_tokens
    FROM hexgate_audit.llm_invocation FINAL
    WHERE received_at >= now() - INTERVAL 30 DAY
    GROUP BY project_id, agent_name, minute
)
WHERE (SELECT count() FROM hexgate_audit.usage_minute) = 0;
