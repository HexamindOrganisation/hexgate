"""Persistence layer for audit events. HTTP-agnostic — exceptions map to status codes in main.py."""
from __future__ import annotations

import json

from clickhouse_connect.driver.client import Client

from schemas import DecisionEvent


class AuditPayloadTooLarge(Exception):
    """Serialized payload column exceeds its per-field byte cap."""

    def __init__(self, field: str, limit: int) -> None:
        super().__init__(f"{field} exceeds {limit} bytes")
        self.field = field
        self.limit = limit


MAX_ARGS_BYTES = 8 * 1024
MAX_HINT_BYTES = 4 * 1024

# Order matches schema.sql; received_at absent (server-stamped via column default).
_DECISION_COLUMNS = [
    "event_id", "occurred_at",
    "project_id", "agent_name", "agent_version_id",
    "session_id", "user_id",
    "tool_name", "role", "outcome", "error_type",
    "reason", "violations", "hint", "arguments",
]

# async_insert batches small inserts; wait_for_async_insert=1 blocks until flush
# so write failures surface synchronously — an audit log must not ack-then-drop.
_DECISION_INSERT_SETTINGS = {
    "async_insert":             1,
    "wait_for_async_insert":    1,
    "async_insert_deduplicate": 1,
}


def insert_decision(
    clickhouse_client: Client,
    *,
    event: DecisionEvent,
    project_id: str,
    agent_version_id: str,
) -> None:
    """Write one decision row to policy_decision.

    Raises AuditPayloadTooLarge on payload overflow and ClickHouseError on
    insert failure; both propagate so the caller maps them to transport errors.
    """
    args_json = json.dumps(event.arguments, default=str) if event.arguments is not None else ""
    hint_json = json.dumps(event.hint, default=str) if event.hint is not None else ""
    if len(args_json.encode("utf-8")) > MAX_ARGS_BYTES:
        raise AuditPayloadTooLarge("arguments", MAX_ARGS_BYTES)
    if len(hint_json.encode("utf-8")) > MAX_HINT_BYTES:
        raise AuditPayloadTooLarge("hint", MAX_HINT_BYTES)

    row = [
        event.event_id,
        event.occurred_at,
        project_id,                # bearer-resolved
        event.agent_name,
        agent_version_id,          # platform-resolved
        event.session_id,
        event.user_id,
        event.tool_name,
        event.role,
        event.outcome,
        event.error_type,
        event.reason,
        list(event.violations),
        hint_json,
        args_json,
    ]
    clickhouse_client.insert(
        "policy_decision",
        [row],
        column_names=_DECISION_COLUMNS,
        settings=_DECISION_INSERT_SETTINGS,
    )


# --- Read path: dashboard aggregation ----------------------------------------
# Query-time GROUP BY over policy_decision; no rollups/materialized views (v1).
# The table's sort key (project_id, agent_name, outcome, occurred_at) and
# LowCardinality columns make these scans cheap. All time-axis logic keys off
# occurred_at (event time), never received_at.

# Selectable dashboard windows → hours. The 90-day TTL is the hard ceiling.
WINDOW_HOURS: dict[str, int] = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}

# Time-bucket granularity per window, chosen to keep the series at ~24-30 points
# regardless of range (24h→hourly, 7d→6h, 30d→daily).
_WINDOW_BUCKET_MINUTES: dict[str, int] = {"24h": 60, "7d": 360, "30d": 1440}

# Empty agent/role render as this in breakdowns (a real "no role" bucket, not
# dropped). The list endpoint translates it back to "" when filtering.
NO_VALUE_LABEL = "(none)"

_OUTCOMES = ("allow", "deny", "needs_approval")


def bucket_minutes_for(window: str) -> int:
    """Bucket size (minutes) for a window key. KeyError on unknown window —
    callers validate the window against WINDOW_HOURS first."""
    return _WINDOW_BUCKET_MINUTES[window]


def _zero_counts() -> dict[str, int]:
    return {"all": 0, "allow": 0, "deny": 0, "needs_approval": 0}


# One round trip: grand total + per-outcome + per-(agent|role|tool, outcome).
# GROUPING(col)=1 marks a column rolled up for that row, so we classify each row
# by which grouping set produced it rather than trusting the (default) value of a
# rolled-up column. Every set except () keys on outcome, so g_outcome=1 uniquely
# identifies the grand-total row.
_SUMMARY_SQL = """
SELECT
    agent_name, role, tool_name, outcome,
    GROUPING(agent_name) AS g_agent,
    GROUPING(role)       AS g_role,
    GROUPING(tool_name)  AS g_tool,
    GROUPING(outcome)    AS g_outcome,
    count() AS n
FROM policy_decision
WHERE project_id = {pid:String}
  AND occurred_at >= now() - INTERVAL {hrs:UInt32} HOUR
GROUP BY GROUPING SETS (
    (),
    (outcome),
    (agent_name, outcome),
    (role, outcome),
    (tool_name, outcome)
)
"""


def summarize(client: Client, *, project_id: str, since_hours: int) -> dict:
    """Totals and categorical breakdowns for a project over the window.

    Returns ``{totals, by_agent, by_role, by_tool}`` where ``totals`` is a
    counts dict and each breakdown is a list of ``{key, all, allow, deny,
    needs_approval}`` sorted by ``all`` descending. Empty agent/role keys map to
    NO_VALUE_LABEL. An empty window yields zeroed totals and empty breakdowns.
    """
    result = client.query(
        _SUMMARY_SQL, parameters={"pid": project_id, "hrs": since_hours}
    )

    totals = _zero_counts()
    by_agent: dict[str, dict[str, int]] = {}
    by_role: dict[str, dict[str, int]] = {}
    by_tool: dict[str, dict[str, int]] = {}

    def _add(store: dict[str, dict[str, int]], key: str, outcome: str, n: int) -> None:
        bucket = store.setdefault(key or NO_VALUE_LABEL, _zero_counts())
        bucket["all"] += n
        if outcome in bucket:
            bucket[outcome] += n

    for agent, role, tool, outcome, g_agent, g_role, g_tool, g_outcome, n in result.result_rows:
        n = int(n)
        if g_outcome:  # only the () grand-total set rolls up outcome
            totals["all"] = n
        elif g_agent and g_role and g_tool:  # (outcome) set
            if outcome in totals:
                totals[outcome] = n
        elif not g_agent:  # (agent_name, outcome)
            _add(by_agent, agent, outcome, n)
        elif not g_role:  # (role, outcome)
            _add(by_role, role, outcome, n)
        elif not g_tool:  # (tool_name, outcome)
            _add(by_tool, tool, outcome, n)

    def _ranked(store: dict[str, dict[str, int]]) -> list[dict]:
        return sorted(
            ({"key": k, **v} for k, v in store.items()),
            key=lambda r: r["all"],
            reverse=True,
        )

    return {
        "totals": totals,
        "by_agent": _ranked(by_agent),
        "by_role": _ranked(by_role),
        "by_tool": _ranked(by_tool),
    }


_TIMESERIES_SQL = """
SELECT
    toStartOfInterval(occurred_at, INTERVAL {bucket:UInt32} MINUTE) AS t,
    outcome,
    count() AS n
FROM policy_decision
WHERE project_id = {pid:String}
  AND occurred_at >= now() - INTERVAL {hrs:UInt32} HOUR
GROUP BY t, outcome
ORDER BY t
"""


def timeseries(
    client: Client, *, project_id: str, since_hours: int, bucket_minutes: int
) -> list[dict]:
    """Per-bucket outcome counts. Returns ``[{bucket, allow, deny,
    needs_approval}]`` ordered by bucket. Sparse: buckets with no events are
    absent (the chart can gap-fill); an empty window returns ``[]``."""
    result = client.query(
        _TIMESERIES_SQL,
        parameters={"pid": project_id, "hrs": since_hours, "bucket": bucket_minutes},
    )
    points: dict[object, dict] = {}
    for t, outcome, n in result.result_rows:
        point = points.setdefault(t, {"bucket": t, "allow": 0, "deny": 0, "needs_approval": 0})
        if outcome in point:
            point[outcome] = int(n)
    return [points[t] for t in sorted(points)]


def _decode_json_column(raw: str) -> object:
    """hint/arguments are stored as JSON strings ("" for null). Decode back to
    objects for the API; leave malformed values as the raw string rather than
    failing the whole read."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


_LIST_COLUMNS = (
    "event_id, occurred_at, received_at, agent_name, agent_version_id, "
    "session_id, user_id, tool_name, role, outcome, error_type, "
    "reason, violations, hint, arguments"
)


def list_decisions(
    client: Client,
    *,
    project_id: str,
    since_hours: int,
    agent: str | None = None,
    role: str | None = None,
    outcome: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict:
    """Filterable detail rows for the events table, newest first by occurred_at.

    ``agent``/``outcome`` match exactly; ``role`` matches exactly too — pass ""
    to select the no-role bucket (the endpoint maps NO_VALUE_LABEL → ""). Returns
    ``{rows, total, limit, offset}`` where ``total`` is the unpaginated match
    count (for pager state) and each row has hint/arguments decoded to objects.
    """
    where = [
        "project_id = {pid:String}",
        "occurred_at >= now() - INTERVAL {hrs:UInt32} HOUR",
    ]
    params: dict[str, object] = {"pid": project_id, "hrs": since_hours}
    if agent:
        where.append("agent_name = {agent:String}")
        params["agent"] = agent
    if role is not None:
        where.append("role = {role:String}")
        params["role"] = role
    if outcome:
        where.append("outcome = {outcome:String}")
        params["outcome"] = outcome
    where_sql = " AND ".join(where)

    total = client.query(
        f"SELECT count() FROM policy_decision WHERE {where_sql}", parameters=params
    ).result_rows[0][0]

    page_params = {**params, "lim": limit, "off": offset}
    result = client.query(
        f"SELECT {_LIST_COLUMNS} FROM policy_decision WHERE {where_sql} "
        "ORDER BY occurred_at DESC LIMIT {lim:UInt32} OFFSET {off:UInt32}",
        parameters=page_params,
    )
    rows = []
    for raw in result.result_rows:
        row = dict(zip(result.column_names, raw))
        row["violations"] = list(row.get("violations") or [])
        row["hint"] = _decode_json_column(row.get("hint") or "")
        row["arguments"] = _decode_json_column(row.get("arguments") or "")
        rows.append(row)

    return {"rows": rows, "total": int(total), "limit": limit, "offset": offset}
