# R-GUARD-007: Apply the guard stance from the signed bundle in the guarded runner, closed-world at construction

**Status:** Accepted · 2026-09-29
**Applies to:** `hexgate/security/bundle.py`, `hexgate/guards/runner.py`, `hexgate/guards/stance.py`, `hexgate/agents/factory.py`, `hexgate/adapters/**/wrapper.py`

## Decision

The enable/disable stance a policy authors in its `guards:` block (R-GUARD-006) is
carried in the signed bundle manifest and **applied per call in the guarded runner**:
the runner skips a guard the current policy disables for that tool. A **fail-fast
closed-world check runs once at construction** against the agent's declared guards.

- The stance MUST ride the **signed** bundle manifest as a `guards` section derived
  from the resolved policy at compile, so it is tamper-proof and readable on the
  opaque-WASM path (there is no readable `AgentPolicy` at runtime). It MUST be read
  back through a `PolicyBundle` method that defaults **safe — guard enabled** — when
  the section is absent, so an older bundle keeps every guard running.
- The stance MUST be applied in `hexgate.guards.runner` around `decide`: for each
  before- and after-guard, the runner reads `enforcer.policy.effective_guards(tool)`
  and skips a guard the policy set `enabled: false`. This is the same skip already
  used for a guard's tool-name reach, so before- and after-guards are gated
  identically. One shared pipeline is installed on every tool; nothing is filtered at
  build time.
- Because the stance is read from the **current** engine on every call, and
  `refresh()` swaps `enforcer.policy` in place, a policy change takes effect on the
  **next call**, uniformly across every framework (native and the runner-driven
  adapters route their guarded calls through the same runner).
- The stance is **agent-level** and, in v1, **baseline-only** (R-GUARD-006): the bundle
  carries one baseline stance (`PolicySet.guard_stance`). The baseline is uniform — it
  applies to every tool and every caller — so the only requirement is that every role
  resolve to the **same baseline**. A policy whose roles set different baselines MUST fail
  loud, never silently pick one (a role silent on a baseline guard runs it, so silence and
  an explicit `enabled: false` genuinely diverge). This is a plain equality check with no
  reachability and therefore no fail-open. Per-tool (and per-caller) governance is deferred
  to v2, precisely because folding per-role per-tool overrides into one agent-level stance
  cannot be done without a fail-open — see R-GUARD-006's Why.
- **Closed-world at construction:** a policy that, when the agent is built, names a
  guard the agent does not declare (or a name attached more than once) MUST stop cold
  (raise). A guard the policy does not name defaults to **enabled** (runs as coded). The
  check MUST run against the guards actually installed on the agent (the stamp when a
  re-enforce omits `guards=`), not the raw argument — else re-enforcing a stamped agent
  without restating its guards would wrongly stop cold.
- The check MUST run **once**, when the agent (or, for the per-run runner-driven
  adapters, its cached policy binding) is first resolved — **never on the per-run wrap**,
  which re-runs after every refresh.
- A **later** refresh that names an unknown guard MUST NOT crash the running agent: it
  degrades to a safe no-op (the name matches no guard), and the authoring lint
  (R-GUARD-006/PR4, `hexgate policy validate`) is the guard against the typo.
- An empty stance MUST NOT change the signed bytes: a guards-free policy's manifest
  and `source_hash` stay byte-identical to before the block existed (omit the
  section), so no stored bundle drifts.

## Why

The runtime engine is an opaque compiled `PolicyBundle` on both the platform path and
the local signed-bundle path; `effective_guards` exists only on the readable
`AgentPolicy`, which runtime never sees. So the stance has to be precomputed and
signed into the manifest — the same mechanism `agent_gating` uses for admission and
reach (R-AGENT-002). Carrying it outside the signature would let an attacker flip a
guard off in transit; a guard silently disabled in flight is the worst failure this
whole subsystem exists to prevent.

Applying it **in the runner, per call**, is what makes governance actually work: the
guarded runner is the one seam every framework passes through, and it reads the live
`enforcer.policy` each call, so a secadmin's toggle in the dashboard takes effect on
the next turn for a native agent and a runner-driven one alike. The cost is a single
dict lookup per guard per call — negligible, and paid only where guards run. Baking
the stance into per-tool pipelines at construction (the first design) was a premature
optimization: it saved that lookup but split the behaviour by framework (native baked
once; the OpenAI/Google runners re-wrapped per turn) and made a native agent's guard
toggle require a reconstruct. The runner check removes that asymmetry entirely.

Agent-level, because the bundle carries one stance; a per-role stance is not
representable when roles disagree, so the compile fails loud rather than enforce one
role's "disabled" on callers of another — a fail-open in the direction that matters.

Closed-world **at construction**, because the manifest is the contract and a policy
that governs a guard the agent never declared is a config error worth catching at
build. But a *live* refresh is different: crashing a running agent because a dashboard
edit typo'd a guard name would be its own outage, so at refresh time an unknown name
is a no-op and the authoring lint carries the burden of catching it.

The empty-stance byte-identity requirement continues R-GUARD-006's `_drop_empty_guards`
rule into the bundle: a project that never touches guards must not see its bundle
`source_hash` move, or the AI Act report reads a `MATRIX_SOURCE_DRIFTED` for an edit
nobody made.

## Consequences

- A guard enable/disable change is live on the **next call** after a refresh, on every
  framework — no reconstruct, no per-framework caveat.
- `PolicyBundle`/`PolicySet` expose `effective_guards(tool)` (read per call) and
  `governed_guard_names()` (the construction check); `build_signed_bundle` gains the
  producer step; `guards/stance.py` holds only the construction-time validation.
- The runner does a per-call `effective_guards(tool)` lookup when an engine is present;
  empty for the guards-only path or an engine without the reader (every guard runs).
- Authoring lints (a guard rule matching no declared guard, a redundant override) land
  in PR4 as the ergonomic ahead of the construction stop-cold and the live no-op.
- A divergent guard stance MUST surface at policy save/validate as a 422, not be swallowed.
  `guard_stance()` is lazy on the `PolicySet`, so `analyze_policy` forces it and reports a
  `guard-divergence` lint, which the single-document save/validate route treats as blocking
  (`_BLOCKING_FINDINGS`) — before any compile — so a divergent baseline is rejected
  rather than stored to crash the SDK later. The modular path needs no equivalent gate: a
  `guards:` block is not module-composable (rejected at link), so a resolved modular policy
  never carries a stance to diverge. `compile_bundle` itself MUST still degrade a
  `PolicySetError` to no-bundle like any compile failure: `backfill_bundles` /
  `recompile_project` call it without a try and depend on that fail-safe (never fail the
  boot; keep live bundles all-or-nothing, R-POL-002).
- `guard_stance()` is computed once and memoized — including a `PolicySetError`, which is
  cached and re-raised, so a live divergent fallback engine does not recompute (and re-log
  a warning) on every guarded call.
- The stance is source-agnostic: a guard block authored in the **compose** `policy.yaml`
  entry file (R-GUARD-006) lowers to the same resolved `AgentPolicy.guards`, injected onto
  every role identically, so `guard_stance()` / the bundle read it the same way as a classic
  single-file policy — and because it is agent-level, the cross-role divergence path cannot
  trigger from a compose-resolved policy.

## Rejected alternatives

- **Bake the stance into per-tool pipelines at construction.** Saves a per-call lookup
  but splits behaviour by framework (native bakes once; framework runners re-wrap per
  turn) and makes a native agent's toggle need a reconstruct. The uniform runner check
  costs one dict lookup and removes the asymmetry.
- **A per-role stance applied per request.** Not representable when roles disagree; v1
  governs the agent, not the caller.
- **Carry the stance beside the signature, unsigned.** An attacker could disable a
  guard in transit; it must be signed like `agent_gating`.
- **On role divergence, take the default role's stance.** Silently enforces a stance
  the operator did not author for the other roles.

## Verify

```
pytest tests/security/test_guard_policy.py tests/agents/test_enforce_policy_guards.py -k "guard or stance or runtime"
```
