# R-USAGE-002: Per-user usage extends R-USAGE-001, keyed on the caller-asserted `user_id`, project-wide

**Status:** Accepted · 2026-10-21 · extends R-USAGE-001
**Applies to:** `hexgate/security/enforcer.py`, `hexgate/security/usage_source.py`,
`hexgate/adapters/_common.py`, `hexgate/runtime/agent_usage.py`,
`hexgate/security/decision.py`, `hexgate/security/agent_gate.py`, `hexgate/audit.py`,
`hexgate/cloud/client.py`, `platform/api/hexgate_api/features/usage/**`,
`platform/clickhouse/migrations/0006_*`, `docs/policy/constraints.mdx`

## Decision

`user_usage.<metric>_<window>` counts one end user, `HexgateContext.user_id` as the
caller asserts it, across every agent of the project. It is the agent machinery of
R-USAGE-001 with a second scope kind, and adds only what a user key needs:

- `user_usage.*` MUST inherit every rule of R-USAGE-001: snapshot plus ledger with the
  ingest margin, fail open by default, the fail mode per path, one signed
  `on_unavailable` per agent, soft. Users MUST get their own refresh thresholds
  (30 / 60 / 300 s by default, `HEXGATE_USAGE_USER_*`).
- The key MUST be `HexgateContext.user_id` exactly as the caller set it. The docs MUST
  state the trust rule: derive it from the authenticated session, never from a value
  the end user sends. A verified tier MUST arrive as an additive `usage.user_identity`
  policy key, and `sec_ai.usage_identity` MUST say which tier a decision used (today
  always `asserted`).
- An empty or absent `user_id` MUST read state `unavailable`, in every mode. No ledger,
  snapshot or rollup row may exist for `""`.
- The scope MUST be project-wide, across agents. The docs and the `write-policy` skill
  MUST recommend pairing every user cap with an `agent_usage.*` ceiling.
- The user fetch MUST happen only when the policy references `user_usage.*` and the
  context has a `user_id`, and it MUST join the concurrent run-boundary fetch, on its
  own worker, never after the agent's.
- `user_id` MUST NOT appear in a URL or an SDK log line. Logs print a fingerprint and
  the exception class, never a user failure's message.
- A user fetch failure MUST pause every user's fetch, not only that user's. A user ID
  the platform would refuse MUST NOT be sent.
- Agent reads MUST NOT wait behind user reads on the platform, where user reads have
  their own pool and memo. In the SDK, an agent's run-boundary fetch MUST NOT wait
  behind user fetches: it runs on the boundary's own worker. Background refreshes MAY
  share the source's pool, so a burst of stale users can delay an agent's background
  refresh; that only ages its snapshot until the next run boundary fetches it.
- Each decision MUST carry its per-scope states (`Decision.usage_states`;
  `sec_ai.usage_state` for the agent, `sec_ai.user_usage_state` for the user). A denial
  MUST expose `quota_scopes`, the usage roots its failing constraints reference, on the
  `Decision` and on `AgentNotAdmittedError`. Neither MAY reach the model's error payload.
- Both engines MUST report every failing constraint (`Verdict.failed_constraints`), so
  `quota_scopes` doesn't depend on which engine decided. It MUST be derived from
  constraint sources, never parsed out of `reason`.

## Why

**The noisy neighbour.** `agent_usage.*` is one counter per agent, shared by everyone
who uses it, so one heavy user can exhaust it for all of them. That was the main
objection to the agent series. A per-user counter fixes it, and it can only be built
from the same pieces: decisions stay in-process (R-USAGE-001), so a user's count has
to be pulled to the SDK like an agent's.

**Caller-asserted is the trust anchor we already have.** Hexgate never authenticates
end users; the customer's application does, and tells hexgate who the user is. User
bans already rely on exactly that `user_id`. Waiting for a signed identity tier would
block the feature on an unrelated project, and a signed tier would not change the
threat model for the common case, where the customer's server sets the context from
its own session. What breaks the cap is an integrator who copies the ID from the
request body: a user who can choose their ID can rotate it, and every rotated ID
starts at zero. The docs have to say this loudly, because nothing in the SDK can
detect it. `sec_ai.usage_identity` records the tier now so that a verified tier can be
added later without anyone having to guess what old decisions relied on.

**No anonymous bucket.** If an empty `user_id` were a user, every anonymous caller
would share one counter: strangers would deny each other, and one row would grow with
all anonymous traffic. So the empty ID is "no usage to read", and the fail mode
decides: zeros under `allow`, failing closed under `deny`. This holds in local mode
too, which is why it is a state (`unavailable`), not a platform condition.

**Project-wide, and paired with a ceiling.** A per-(agent, user) scope would let a
user run N agents at N times the cap, which is not what "each user's share" means to
the people writing these policies. The cost is that a cheap agent's runs count
against an expensive agent's cap; the docs say so. The ceiling is what bounds rotation
and anonymous traffic, so a user cap without one is a cap only against honest users.

**Cold users and a shared back-off.** Unlike agents, most user fetches are for a key
the process has never seen, so a per-scope back-off never kicks in: during a platform
outage every new user would wait the full 2-second timeout. Pausing the whole kind
after one failure means a slow platform costs the first cold user, not all of them.
Joining the concurrent boundary fetch, on a separate worker, keeps a cold user's wait
in parallel with the policy, ban and agent fetches rather than after them. Admitting
a cold user optimistically and fetching in the background would let a scripted user
round-robin new processes for free.

**Who hears about a denial.** A user over quota is a 429 to that user; an agent over
quota is an ops alert. The app can only tell them apart if the denial says which root
failed. The pydantic engine reported the failing constraint only as text inside
`reason`, and parsing that would break on any wording change, so both engines now
carry the failing constraint sources in a field the model never sees. The pydantic
engine evaluates every constraint on a deny, because the WASM engine already reports
every violation: otherwise a run over both caps would name one root on one engine and
two on the other. The roots a failing constraint *references* are reported, not which
side of an `or` failed: that is what can be computed from the source without
re-evaluating, and the docs say so.

**Overshoot is per user, and small.** A single session with sticky routing is counted
by its own process's ledger, so it overshoots by about zero. A scripted user
round-robining N pods at r runs/s overshoots by about (N − 1) × r × (30 s + lag):
4 pods at 0.5 runs/s is about 50 runs. A cold user adds none, because the run waits
for the fetch. Rotation is not overshoot but a bypass, bounded only by the agent
ceiling.

## Consequences

- A cold user's run waits for one fetch (2 s worst case), in parallel with the other
  run-start fetches. Usage-free and agent-only policies pay nothing new.
- Each user keeps the full ledger tiers, about 118 KB per user, bounded by
  `HEXGATE_USAGE_USER_CACHE`. A short tier for platform-backed users needs its own
  decision: the ledger registry is process-wide while "platform configured" is per
  enforcer.
- The platform has to be deployed with the user rollup (`0006`) and the user read
  before an SDK with this wiring meets traffic; otherwise every user reads
  `unavailable`, and under `deny` every user is refused.
- `Decision(usage_state=…)` construction no longer works; `decision.usage_state` reads
  the agent's state.

## Rejected alternatives

- **Wait for the signed identity tier.** It blocks a fair-use feature on an
  unrelated project and doesn't change the threat model when the server sets the ID.
- **A shared `""` bucket.** Strangers deny each other (see Why).
- **A per-(agent, user) scope.** A user gets N times the cap across N agents.
- **Optimistic admission of cold users.** Free bursts for anyone who rotates
  processes or IDs.
- **A per-scope `on_unavailable` map.** No one has asked for different fail modes per
  root; the single value keeps the signed manifest and the docs simple. Additive later.
- **Retry-after on a denial.** Not derivable from summed minute buckets without a
  second query per denial.
- **Parsing `reason` for the failing constraint.** It breaks on any wording change,
  and the model's payload must not grow.

## Verify

```
uv run pytest tests/security/test_enforcer_user_usage.py tests/security/test_usage_source.py \
  tests/adapters/test_common.py tests/security/test_engine_parity.py -q
```
