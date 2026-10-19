# R-USAGE-001: Enforce cross-run usage as a soft quota that fails open by default

**Status:** Accepted · 2026-10-12
**Applies to:** `hexgate/runtime/agent_usage.py`, `hexgate/security/usage_source.py`,
`hexgate/security/enforcer.py`, `hexgate/adapters/_common.py`,
`hexgate/security/bundle.py`, `hexgate/security/policy_set.py`,
`hexgate/security/models.py`, `hexgate/security/compose/**`,
`platform/api/hexgate_api/features/usage/**`, `docs/policy/constraints.mdx`

## Decision

`agent_usage.*` values are approximate. Each one is a platform snapshot pulled per
agent plus this process's own ledger. When the platform can't answer, a value falls
back to the ledger alone, unless the policy opts into `deny`.

- A value MUST be `snapshot + ledger.since(fetched_at − USAGE_INGEST_MARGIN_SECONDS)`.
  The margin MUST stay above the span pipeline's lag. While (snapshot age + margin) is
  within the ledger's fine tier (`FINE_RETENTION_SECONDS`, 300 s), the cut-off is exact
  to the second. Past it (a snapshot older than 270 s at the defaults, still read up to
  `max_staleness_seconds`), the ledger term MUST come from a coarser tier that counts
  the cut-off's whole bucket: an over-count of under one minute, never an under-count.
  The current run is left out of whichever term holds it.
- The usage fetch MUST NOT add a sequential network call at run start. It joins the
  policy and ban fetches concurrently, and a run waits only when the snapshot is
  missing or older than `sync_after_seconds`. The one exception is paths a policy
  refresh adds: they are unknown when the fetches start, so they are refreshed once
  the policy refresh returns, and that call MUST block only when there is no snapshot
  younger than `sync_after_seconds` (in practice, the run that picks up an agent's
  first usage cap). A decision MUST NOT block on a network call; when its reading is
  not fresh it MAY schedule a background refresh, at most one in flight per agent and
  throttled by `min_refresh_seconds`.
- The default fail mode MUST be `allow`: an unavailable snapshot reads ledger-only
  values, never an absent namespace. `deny` MUST be an explicit
  `usage: { on_unavailable: deny }` in the policy, signed into the bundle manifest,
  and omitted from the manifest at the default. A manifest value the SDK doesn't know
  MUST read `allow`, with a warning.
- A path missing from an otherwise usable snapshot MUST follow the fail mode for that
  path alone (`partial`), never for the whole snapshot.
- The fail mode MUST resolve to one value per agent. Roles that disagree MUST fail at
  load (`PolicySetError`), never silently pick one.
- Every decision that reads `agent_usage.*` MUST carry `sec_ai.usage_state`, and that
  state MUST NOT reach the model's error payload.
- Docs MUST present `agent_usage.*` as a soft quota with overshoot, and `run.*` as a
  per-run bound, never as a cross-run alternative. A hard limit MUST NOT be built on
  this pipeline. It needs its own decision record (reserve-then-reconcile, or leases).

## Why

Decisions are made in-process, by an engine that only sees the input it is handed. The
platform never decides, so cross-run state has to be brought to the SDK. Asking a
counter service on every decision would put a network round trip, and a network
dependency, on every tool call. Keeping the hot path local is the property every other
part of the enforcer is built around, so usage is **pulled**: at the run boundary,
alongside the policy and ban fetches, cached between runs, and topped up in the
background when a decision finds it stale.

A snapshot alone is not enough. Spans reach the platform's rollup 5–15 s after they
happen, so a process reading only the snapshot would miss its own last few seconds and
blow straight through its own cap. The process-local ledger closes that gap: it counts this
process's events immediately, and the value is the snapshot plus whatever the ledger
saw after the snapshot was taken.

The ledger term starts **before** the fetch, not at it. If it started at exactly
`fetched_at`, an event that happened just before the fetch but had not yet been ingested
would be in neither term. That is an under-count, so the quota would fail open by
exactly the amount the pipeline was behind. Starting the ledger term
`USAGE_INGEST_MARGIN_SECONDS` (30 s) earlier means an event can instead be in both terms.
That is an over-count, and it errs in the conservative direction. The margin has to stay
above the pipeline lag for this to hold. The ledger keeps 1-second buckets for 300 s,
which covers (snapshot age + margin) for any snapshot that is refreshing normally. An
older snapshot, kept as last-good while the platform is unreachable, reads the minute
tier instead. There the bucket holding the cut-off counts whole, so the error stays an
over-count of under a minute, in the same conservative direction. The ledger term is
not clipped to a path's window and the snapshot term ends at the fetch, so a value can
also span up to (snapshot age + margin) past its window.

The default is **fail-open** because a soft quota that is stricter on failure than on
success is incoherent. With fail-closed by default, a platform blip (a deploy, a
ClickHouse restart, a network partition) would deny every run of every agent with a
usage constraint. A rate limit would become an outage multiplier. Under `allow`, an
unavailable snapshot reads zeros plus this process's own count. The cap weakens to
per-process for the duration, and no agent stops. `deny` stays available for budgets
where overspend is worse than downtime. Because it is the exception, it has to be
written down: absent means `allow`, and a manifest value an older SDK doesn't recognize
also means `allow`, so a newer builder can never turn an old SDK's blip into an outage.

The fail mode is **per path**, not per snapshot. When a policy update adds a new usage
path, the cached snapshot doesn't have it until the next refresh. Treating the whole
snapshot as unavailable at that moment would, under `deny`, refuse every run until the
refresh lands. That would be an outage caused by publishing a policy. Only the new path
follows the fail mode, and every other path keeps its platform count.

The fail mode is **signed**. An unsigned `deny` could be stripped by whoever delivers the
bundle, and the quota would silently fail open. It rides the manifest like the agent
gating and the guard stance (R-AGENT-002, R-GUARD-007). It is omitted at the default, so
a usage-free policy's bytes and `source_hash` don't move.

The fail mode is **one value per agent** because the snapshot is fetched per agent, not
per role. A role that says nothing reads `allow`. So a policy that sets `deny` on one
role only is ambiguous, and picking either value silently would be wrong for the other
role's callers. A load error forces the author to put the block where every role
inherits it.

`sec_ai.usage_state` is on every decision that read usage, so an operator can tell a
denial on a fresh count from one on a stale or ledger-only count. It stays out of the
model's error payload because nothing about quota state is for the model: as with
`run.*`, budget pressure is never surfaced to the agent.

The guarantee is **soft**, and the docs must say so. Other processes' activity reaches
this one late, by the snapshot's age plus the pipeline lag, so a cap can be passed by
roughly Σ(other processes' rate) × (snapshot age + platform memo + lag). With 4
replicas each admitting 1 run/s, a 5 s refresh and 10 s of lag,
`agent_usage.invocations_1h < 100` can overshoot by about 3 × 1 × 15 ≈ 45 runs at the
moment of crossing. At the 15 s top of the lag range, plus the platform's 1 s memo
(`USAGE_MEMO_TTL_SECONDS`), it is about 3 × 1 × 21 ≈ 63. That is fine for cost control
and fair use. It is wrong for "at most 3 wire transfers per hour, ever", and an author
who reads `< 100` as exact will write exactly that policy. `run.*` is no substitute:
it resets with every run, so it bounds a run, not an hour. An exact cross-run limit
today means `approval_required` on each call or a limiter in the downstream service.

The windows **slide**, so a denial clears as usage ages out. A rate limit is meant to
behave this way, and it is the opposite of `run.*`, whose counters only grow within a
run.

## Consequences

- Overshoot is bounded by concurrency × staleness, not by the limit. It is tuned with
  `HEXGATE_USAGE_REFRESH_SECONDS`, `HEXGATE_USAGE_SYNC_AFTER_SECONDS` and
  `HEXGATE_USAGE_MAX_STALENESS_SECONDS`. Pipeline lag is the floor.
- The enricher stays the dedup boundary for the minute rollup. An over-count when the
  enricher restarts is accepted.
- A quiet process (runs more than `sync_after_seconds` apart) pays one round trip at
  run start, concurrent with the policy and ban fetches. A platform outage costs at
  most the client's 2 s refresh timeout, overlapped with the same fetches; a slow but
  reachable platform can cost up to about 4 s on a process's first fetch, which also
  fetches the signing key. The run
  that picks up an agent's first usage cap pays that round trip after the policy
  fetch instead, not alongside it.
- A future `user_usage.*` reuses this machinery and inherits this decision, plus a
  trust question this record doesn't answer.

## Rejected alternatives

- **A central counter or a reservation per decision** (Redis `INCR`, a Postgres row).
  It is exact, but it puts a network dependency on the hot path and creates a second
  source of truth beside the span pipeline. It is deferred to a hard-limit record.
- **Folding usage into `GET /v1/agents/{name}`.** The numbers change on every request,
  so the policy endpoint would never answer 304 again, and every policy refresh would
  cost a full download.
- **Fail-closed by default.** It multiplies outages (see above).
- **A `limits:` block.** It would be a second authoring surface for what a constraint
  path already expresses, with its own grammar, lint and docs.
- **Calendar windows** (per hour, per day). They need a timezone per project, and they
  cause bursts at every reset.

## Verify

```
uv run pytest tests/security/test_usage_source.py tests/security/test_usage_policy.py \
  tests/security/test_enforcer_agent_usage.py tests/adapters/test_common.py -q
```
