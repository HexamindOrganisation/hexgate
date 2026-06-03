# Audit Pipeline Specification

> Status: living — kept in sync with the audit code. Last reviewed 2026-06.

Scope: the end-to-end path that records every policy decision a Fortify-wrapped
agent makes, from the SDK enforcement point to durable storage in ClickHouse and
the dashboard read view.

This document is descriptive of the current implementation (PR
`gp/feat/sdk_emit_audit_event` + the platform audit endpoint). Where behaviour
is intentionally lossy or POC-grade, it says so explicitly.

---

## 1. Overview

Every time an agent proposes a tool call, the SDK's `PolicyEnforcer` produces a
`Decision` (allow / deny / needs_approval). The audit pipeline ships a copy of
that decision — **out of band, fire-and-forget** — to the platform, which
validates it, resolves server-owned identity fields, and appends one immutable
row to a ClickHouse table. Audit emission is a **side effect of enforcement**:
it never changes, blocks, or fails the decision the agent acts on.

```
┌─────────────────────────── SDK (fortify) ───────────────────────────┐
│  tool call                                                           │
│     │                                                                │
│     ▼                                                                │
│  PolicyEnforcer.decide()  ──►  Decision (event_id, occurred_at)      │
│     │                            │                                   │
│     │ returns to agent  ◄────────┘ (synchronous, authoritative)      │
│     │                                                                │
│     └─► AuditSender.emit(AuditEvent)   (async, best-effort)          │
│              │  bounded concurrency, drop-on-saturation              │
└──────────────┼───────────────────────────────────────────────────────┘
               │  HTTP POST /v1/audit/decisions   (Bearer <fortify_key>)
               ▼
┌─────────────────────── Platform API (FastAPI) ──────────────────────┐
│  require_project      bearer → project_id                            │
│  require_clickhouse   client or 503                                  │
│  validate             clock-skew / retention window                  │
│  resolve              agent_version_id from latest AgentVersion      │
│  insert_decision      byte-cap args/hint, write one row              │
└──────────────┼───────────────────────────────────────────────────────┘
               │  INSERT (async_insert, wait_for_async_insert=1)
               ▼
┌─────────────────────────── ClickHouse ──────────────────────────────┐
│  fortify_audit.policy_decision   MergeTree, monthly partitions,      │
│  TTL 90 days, received_at server-stamped                             │
└──────────────┬───────────────────────────────────────────────────────┘
               │  GET /v1/projects/{id}/audit/{summary,timeseries,decisions}
               ▼
        Project-scoped aggregation endpoints (read API)
```

### Design principles

1. **Enforcement is authoritative; audit is observational.** The `Decision`
   returned to the agent is the source of truth. Audit failures (network down,
   platform 503, saturation) degrade silently and never propagate to the caller.
2. **The server owns identity.** `project_id`, `agent_version_id`, and
   `received_at` are resolved/stamped server-side and are **never trusted from
   the request body**, even though the SDK sends some of them as empty strings.
3. **One envelope, many event types.** The first eight columns/fields are a
   shared "envelope" intended to be reused by future event tables
   (`tool_invocation`, …). `policy_decision` is the first concrete event.
4. **Lossy under pressure, never blocking.** Both the SDK (drop on saturation)
   and the storage layer (byte caps, truncated `arguments`) prefer dropping or
   truncating data over slowing the agent.

---

## 2. The audit record

### 2.1 Stamped at the decision site

`fortify/security/decision.py` — `Decision` is a frozen dataclass. Two fields
exist specifically for audit and are stamped at construction:

| Field | Type | Source |
|-------|------|--------|
| `event_id` | `UUID` | `uuid4()` per decision — the idempotency key end-to-end |
| `occurred_at` | `datetime` (UTC) | `datetime.now(timezone.utc)` at construction |

The remaining decision fields (`agent_name`, `tool_name`, `outcome`, `role`,
`reason`, `error_type`, `violations`, `hint`, `arguments`) are populated by
`Decision.from_verdict()` from the policy engine's `Verdict` plus host context.

### 2.2 Outcome and error_type

```
DecisionOutcome   wire value        error_type (derived)
ALLOW             "allow"           "" (no error tag)
DENY              "deny"            "policy_denied"
NEEDS_APPROVAL    "needs_approval"  "approval_required"
```

### 2.3 Wire payload — `AuditEvent.as_payload()`

`fortify/audit.py` — `AuditEvent` wraps a `Decision` plus the caller identity
read from the active `User` scope (`user_id`, `session_id`). `as_payload()`
produces a flat JSON object whose keys mirror the platform's `DecisionEvent`:

```json
{
  "event_id":    "0b9c…",          // str(UUID)
  "occurred_at": "2026-06-01T13:00:00+00:00",  // ISO 8601, tz-aware
  "agent_name":  "researcher",
  "tool_name":   "read_file",
  "outcome":     "deny",
  "role":        "analyst",        // "" when no role
  "error_type":  "policy_denied",  // "" for allow
  "reason":      "denied for path",
  "violations":  ["v1", "v2"],     // tuple → list
  "hint":        {"glob": "/x/**"},// or null
  "arguments":   {"path": "/etc/passwd"},  // or null; may be truncated upstream
  "user_id":     "alice",          // "" when no User scope
  "session_id":  "sess_1"          // "" when unset
}
```

Server-resolved fields (`project_id`, `agent_version_id`, `received_at`) are
**deliberately absent** from the wire payload.

---

## 3. SDK emission layer

### 3.1 Where emission happens

`PolicyEnforcer.decide()` (`fortify/security/enforcer.py`):

1. Resolve `role` from the active `User` contextvar.
2. Ask the policy engine for a `Verdict`; lift it into a `Decision`.
3. If an `AuditSender` was injected into this enforcer, `emit()` an `AuditEvent`.
4. Return the `Decision` to the adapter (synchronous, unaffected by step 3).

The sender is **injected per enforcer**, not looked up globally — see §3.4.

### 3.2 `AuditSender` — fire-and-forget POST

`fortify/audit.py`. `emit()` is synchronous and non-blocking; it schedules a
background `asyncio.Task` that performs the POST. Key behaviours:

- **Bounded concurrency.** An `asyncio.Semaphore(max_in_flight=32)` caps
  concurrent POSTs.
- **Drop on saturation.** If the semaphore is already exhausted, `emit()`
  increments a dropped counter and returns immediately. A warning is logged on
  the 1st, 101st, 201st… drop (`_dropped % 100 == 1`).
- **No event loop → skip.** If `emit()` is called with no running loop (a sync
  entry point), it no-ops with a one-time warning. Sync agents therefore emit
  no audit unless wrapped in `asyncio.run`.
- **Single 503 retry.** `_send` retries once on HTTP 503 after
  `min(http_timeout, 2.0)`s. Other `>= 400` responses are logged, not retried.
- **Network errors swallowed.** `httpx.RequestError` is logged at WARNING and
  dropped; it never surfaces to the agent.
- **HTTP client:** `httpx.AsyncClient`, 5s timeout, `Authorization: Bearer
  <api_key>` header.

### 3.3 Loop-rebinding safety

asyncio primitives (the semaphore, and httpx's connection pool) bind to the
first event loop that drives them and reject use from any other loop. Because
the sender is a process-global, a process that runs **more than one event loop**
(repeated `asyncio.run`, a job worker, a test suite, a notebook) would otherwise
crash on the second loop. `AuditSender` tracks the loop it is bound to and
**rebuilds its client + semaphore when the running loop changes**, so a reused
sender survives loop rotation. Construction stays eager so `configure()` remains
synchronous.

### 3.4 Configuration & lifecycle

`configure(api_key=None, base_url=None) -> AuditSender | None`:

- Resolves `api_key` from the argument or `FORTIFY_KEY`; returns `None` (audit
  inert) when no key is resolvable.
- Resolves `base_url` from the argument or `FORTIFY_API_URL`, defaulting to
  `http://localhost:8000`. The endpoint is `<base_url>/v1/audit/decisions`.
- **Keyed by api_key.** Senders live in a registry `dict[str, AuditSender]`.
  Calling `configure()` again with the **same** key returns the existing sender
  (idempotent); a **different** key gets its own sender with its own bearer
  token. This is what lets one process audit several tenants/keys correctly.

Each adapter wrapper (`wrap_langchain_agent`, `wrap_openai_agent`,
`wrap_google_agent`, `wrap_pydantic_agent`) and `factory.enforce_policy` call
`configure()` with their resolved key and inject the returned sender into the
`PolicyEnforcer` they build. `bootstrap()` also calls `configure()` (env key) so
local runs work without an explicit key.

`async shutdown()` drains in-flight tasks and closes every sender's HTTP client.
It is safe to call multiple times and is the recommended teardown hook; absent
it, the httpx client is closed by GC at process exit.

| Function | Purpose |
|----------|---------|
| `configure(key, url)` | Get-or-create the sender for `key`. Idempotent per key. |
| `get_sender(key)` | Registry lookup by key (diagnostics). Prefer the injected sender. |
| `shutdown()` | Drain + close all senders. |

---

## 4. Platform ingest endpoint

`POST /v1/audit/decisions` (`platform/api/main.py` → `ingest_decision`).

### 4.1 Request

- **Auth:** `Authorization: Bearer <fortify_key>`. `require_project` verifies
  the key and resolves it to a `project_id`. Missing/invalid → **401**.
- **Body:** `DecisionEvent` (`platform/api/schemas.py`), a pydantic model that
  extends `AuditEnvelope`. Field-level validation (max lengths, enum membership)
  happens here; a malformed body → **422** (FastAPI validation).
- **ClickHouse dependency:** `require_clickhouse` resolves the client and maps a
  connect failure to **503** with `Retry-After: 5`.

### 4.2 Server-side processing

1. **Clock-skew / retention guard.** Reject `occurred_at` more than 5 minutes in
   the future (`CLOCK_SKEW_FUTURE`) or older than the 90-day `RETENTION_WINDOW`
   → **400**.
2. **Resolve `agent_version_id`** = latest `AgentVersion.id` for
   `(project_id, agent_name)`, or `""` if the agent isn't registered. Unknown
   agents still log; the version is just empty.
3. **Insert** via `audit.insert_decision`. `project_id` (bearer-resolved) and
   `agent_version_id` (platform lookup) are passed explicitly and override
   anything in the body.

### 4.3 Responses

| Status | Meaning |
|--------|---------|
| **202 Accepted** | Row written. Body: `{"event_id": "<uuid>"}`. (Sync write, see §5.2 — 202 reflects "queued/durable" semantics but the insert has actually completed.) |
| **400** | `occurred_at` outside the accepted time window. |
| **401** | Missing/malformed/invalid/revoked bearer key. |
| **413** | `arguments` > 8 KiB or `hint` > 4 KiB after JSON serialization. |
| **422** | Body failed schema validation, **or** ClickHouse rejected the row (non-transient — retry won't help). |
| **503** | ClickHouse unreachable or transient insert failure (`OperationalError`). Retryable; carries `Retry-After`. |

### 4.4 Trust boundary

`AuditEnvelope` is intentionally **narrower** than the storage row. The body
carries only `event_id`, `occurred_at`, `agent_name`, `session_id`, `user_id`
(envelope) plus the decision fields. `project_id`, `agent_version_id`, and
`received_at` are server-owned and cannot be spoofed by the SDK.

---

## 5. Storage — ClickHouse

`platform/clickhouse/init/schema.sql`. Database `fortify_audit`, table
`policy_decision`.

> ⚠️ The `init/` directory runs **once on an empty volume**. Editing the schema
> after first boot is ignored — use a real migration runner for changes.

### 5.1 Schema

```sql
CREATE TABLE fortify_audit.policy_decision
(
  -- Envelope (shared with future event tables)
  event_id            UUID,
  occurred_at         DateTime64(3, 'UTC'),
  received_at         DateTime64(3, 'UTC') DEFAULT now64(3),   -- server-stamped
  project_id          LowCardinality(String),
  agent_name          LowCardinality(String),
  agent_version_id    LowCardinality(String) DEFAULT '',
  session_id          String                 DEFAULT '',
  user_id             LowCardinality(String) DEFAULT '',

  -- Decision-specific
  tool_name           LowCardinality(String),
  role                LowCardinality(String) DEFAULT '',
  outcome             Enum8('allow'=1, 'deny'=2, 'needs_approval'=3),
  error_type          LowCardinality(String) DEFAULT '',
  reason              String,
  violations          Array(String),
  hint                String CODEC(ZSTD(3)),
  arguments           String CODEC(ZSTD(3))  -- SDK-truncated JSON; may be lossy
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(occurred_at)
ORDER BY (project_id, agent_name, outcome, occurred_at)
TTL toDateTime(occurred_at) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;
```

- **`occurred_at`** is event time (SDK), **`received_at`** is ingest time
  (server default). Reads order by `received_at`; retention/partitioning key off
  `occurred_at`.
- **Sort key** `(project_id, agent_name, outcome, occurred_at)` optimizes the
  expected query shape: "decisions for a project/agent, filtered by outcome,
  newest within a window."
- **`hint` / `arguments`** are stored as ZSTD-compressed JSON strings, not native
  JSON, and are documented as potentially lossy (`arguments` is SDK-truncated;
  see §6).
- **TTL 90 days** — rows self-expire, consistent with the ingest retention guard.

### 5.2 Insert semantics

`platform/api/audit.py` — `insert_decision`:

- **Byte caps before write:** `arguments` JSON ≤ 8 KiB, `hint` JSON ≤ 4 KiB,
  else `AuditPayloadTooLarge` → 413. `None` serializes to `""`.
- **Insert settings:** `async_insert=1`, `wait_for_async_insert=1`,
  `async_insert_deduplicate=1`. Small inserts are batched server-side, but the
  call **blocks until the batch flushes**, so a write failure surfaces
  synchronously rather than being acked-then-dropped — an audit log must not
  silently lose acknowledged rows.
- **Dedup:** `async_insert_deduplicate` plus the unique `event_id` provides
  idempotency across SDK retries (the single 503 retry, or any at-least-once
  delivery): re-POSTing the same `event_id` does not create a duplicate row.

---

## 6. Privacy & data-handling notes

- **`arguments` carries raw tool inputs** (paths, payloads, possibly secrets or
  PII). It is transmitted to the platform and stored (compressed) for up to 90
  days. The default `base_url` is **plaintext `http://localhost:8000`**;
  production deployments must set `FORTIFY_API_URL` to a TLS endpoint. Consider
  redaction/allowlisting of `arguments` before relying on this in production.
- **Truncation is lossy and asymmetric.** The SDK may already truncate
  `arguments`; the platform additionally **rejects** (413) oversize payloads
  rather than truncating. An over-cap decision is therefore *not stored at all*
  unless the SDK trims it first.

---

## 7. Read path — aggregation endpoints

The raw `GET /v1/audit/decisions?limit=N` debug dump has been **removed**.
Reads are now project-scoped aggregation endpoints that group server-side in
ClickHouse (query-time `GROUP BY`; no rollups/materialized views). The table's
sort key `(project_id, agent_name, outcome, occurred_at)` and `LowCardinality`
columns make these scans cheap. All time-axis logic keys off `occurred_at`
(event time), never `received_at`. See `platform/api/audit.py` (`summarize`,
`timeseries`, `list_decisions`).

| Endpoint | Returns |
|----------|---------|
| `GET /v1/projects/{id}/audit/summary?window=` | Totals + denial counts, plus breakdowns by agent / role / tool (one `GROUPING SETS` query). |
| `GET /v1/projects/{id}/audit/timeseries?window=` | Per-bucket outcome counts (`toStartOfInterval`); bucket size tracks the window. |
| `GET /v1/projects/{id}/audit/decisions?window=&agent=&role=&outcome=&limit=&offset=` | Filterable detail rows, newest first, with `total` for pagination; `hint`/`arguments` decoded back to objects. |

- **`window`** is `24h` / `7d` / `30d` / `90d`, validated by a `Literal` (bad
  value → 422) and bounded by the 90-day storage TTL. `role="(none)"` selects
  the empty-role bucket.
- **Concurrency.** A client firing several of these reads at once (e.g. a
  dashboard loading summary + timeseries + decisions together) would otherwise
  hit "concurrent queries within the same session". The shared, process-global
  ClickHouse client is created with `autogenerate_session_id=False`
  (`platform/api/clickhouse.py`) so the thread-safe HTTP pool serves concurrent
  queries in parallel; this also hardens the ingest path under load.

> Still POC-grade on **auth**: the endpoints are project-scoped (gap #1 partly
> closed) but not yet gated behind the `read_audit` scope — they carry a
> `TODO(auth)` marker, matching the unauth posture of the other dashboard reads
> (`/agents`, `/tokens`). Scope enforcement must land before exposure beyond
> local development.

---

## 8. Failure-mode summary

| Failure | SDK behaviour | Agent impact |
|---------|---------------|--------------|
| No api_key configured | `configure()` returns `None`; no sender injected | none (audit inert) |
| No running event loop | `emit()` no-ops, one-time warning | none |
| Sender saturated | event dropped, periodic warning | none |
| Platform returns 503 | one retry, then network-error log | none |
| Platform returns 413/422/400 | logged as ingest error (`>= 400`) | none |
| Network unreachable | `RequestError` logged, dropped | none |
| Event loop rotates | client + semaphore rebuilt transparently | none |

| Failure | Platform behaviour |
|---------|--------------------|
| ClickHouse unreachable | 503 + `Retry-After` (startup logs a warning, does not crash) |
| Transient insert error | 503 + `Retry-After` |
| Storage rejects row | 422 (retry won't help) |
| Oversize args/hint | 413 |
| Bad/missing bearer | 401 |
| occurred_at out of window | 400 |

---

## 9. Open items / known gaps

1. **Read path auth** — the read endpoints are now project-scoped (§7), but
   still **unauthenticated**: they need the `read_audit` scope gate (a
   `TODO(auth)` marker is in place). `project_id` filtering is done.
2. **`arguments` redaction** — no field-level redaction before transmit/store;
   relies on byte caps only.
3. **Default transport is plaintext HTTP** — safe only for localhost; require
   TLS via `FORTIFY_API_URL` elsewhere.
4. **Sync agents emit nothing** — `emit()` requires a running loop; sync entry
   points silently produce no audit.
5. **Schema evolution** — `init/schema.sql` runs once; there is no migration
   runner wired up yet.
6. **At-least-once, not exactly-once end to end** — the SDK can drop on
   saturation/network failure (audit is best-effort); `event_id` dedup prevents
   duplicates but not gaps.
</content>
</invoke>
