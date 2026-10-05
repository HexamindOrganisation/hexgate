# R-GUARD-006: The `guards:` policy block enables/disables declared guards, baseline-only in v1

**Status:** Accepted · 2026-09-29
**Applies to:** `hexgate/security/models.py`, `hexgate/security/policy_set.py`, `hexgate/security/linker.py`, `hexgate/security/__init__.py`, `hexgate/security/compose/**`

## Decision

A policy governs which of an agent's manifest-declared guards run through a
`guards:` block. Version 1 is enable/disable only, and **baseline-only** — the stance
governs the agent, not a tool or a caller.

- A top-level `guards:` mapping is the **baseline**: a guard name (the
  `GuardManifest.name` in `hexgate/manifest/models.py`) maps to `{enabled: <bool>}`.
  It applies agent-wide — the same stance for every tool.
- The block MUST be accepted **only on the policy baseline**. It MUST be rejected on a
  `tools:` entry and on `default_policy` / `admission:` / `agents:` / `skills:` entries,
  loud, with a message naming the fix (move it to the baseline) — never silently ignored.
- A manifest-declared guard the policy does not mention MUST default to **enabled**
  (it runs as the agent's code attached it).
- `effective_guards(tool_name)` returns the baseline stance; `tool_name` is accepted for
  a uniform signature with the bundle mirror (R-GUARD-007) but does not change the result.
- Baseline `guards` MUST merge per key, last-wins, exactly like `tools` (a child or
  later mixin re-declaring a guard's rule overrides the parent's).
- **Per-tool and per-caller guard governance is deferred to v2** (see Why): v1 carries
  no `(tool, guard)` overrides.
- Module composition MUST NOT silently accept `guards` in v1: the fold composes
  only tool decisions (via `effective_tools`), which guards are not, so a module
  (boundary/capability) that sets `guards:` MUST be rejected fail-loud, like any
  field outside `_MODULE_COMPOSABLE_FIELDS` in `hexgate/security/linker.py`
  (`skills:` is composable, since it lowers to `skill*:` keys). Composing guards
  across modules is deferred.
- **Compose authoring surface** (`hexgate/security/compose/`, the `policy.yaml` entry
  file the platform edits): `guards:` is a block keyword at the **top level** (every
  agent) and in an **agent body** (that agent), merged last-wins per name. It is the
  agent's single stance, so it MUST be rejected in a **role body**, in a **boundary**,
  and in an **imported fragment** (guards are not composable). It lowers to the resolved
  `AgentPolicy.guards` — injected onto every folded role identically after the link, so
  the one agent-level stance can never diverge across roles. Downstream (bundle, runtime,
  closed-world) is identical to the classic single-file surface — same `AgentPolicy.guards`.
- The block MUST NOT lower into `effective_tools` and MUST NOT emit Rego/WASM. It
  is not an allow/deny decision; the enable/disable stance is applied when the
  guard pipeline is built, not on every tool call.
- Version 1 MUST NOT carry guard parameters. The plugin code owns its parameters;
  the policy only toggles a guard on or off. The `{enabled: bool}` value is a model
  (not a bare bool) so a later version can add fields without a grammar break.
- The "manifest is the contract" closed-world check (a guard named in a policy but
  absent from the agent's manifest → stop cold) is enforced where the manifest is
  available — registration / enforcement (PR3, R-GUARD-007) — NOT at policy-parse
  time, which has no manifest.

## Why

A guard is a runtime pre/post hook around a tool call, not an authorization
decision. The tool blocks (`tools:`, `agents:`, `skills:`, `admission:`) all lower
into `effective_tools` because each yields a per-call `DecisionOutcome` the engine
evaluates. A guard toggle yields no such outcome: whether `secret_guard` runs on
`send_email` is fixed the moment the pipeline is assembled, so encoding it as a
synthetic tool key would be a category error — it would turn a build-time toggle
into a per-call allow/deny, add engine latency to something with none, and pollute
the closed-world tool namespace with keys that never decide anything. So the block
stays out of the decision engine and is read at pipeline-build time instead.

Enable/disable-only is a deliberate v1 boundary. The behaviour of a guard (what it
scans for, how it redacts, its thresholds) lives in the plugin code; letting a
policy also set parameters would split one guard's logic across two authorities
that can disagree, and there is no product need yet. A secadmin's actual v1 need is
coarser and safer: see every declared guard and turn one off (or back on) for the
agent. The baseline serves exactly that.

**Baseline-only is also a deliberate v1 boundary, and a safety one.** The guard stance
is *agent-level*: the pipeline is built once and one stance is signed into the bundle
(R-GUARD-007), applied to every caller. A per-tool override authored under one role
therefore cannot be represented per-caller; folding several roles' per-tool overrides
into that single stance is inherently lossy, and every reconciliation rule we tried
(strict cross-role equality, naive merge, "roles that list the tool must agree") either
rejected legitimate policies or, worse, let one role's *disable* silently apply to a
caller that never disabled the guard — a fail-open in the direction this subsystem exists
to prevent, because whether a role can even reach a tool depends on the full
`effective_tools` computation (default_policy, lowered agent/skill grants), not the
authored `tools:` list. The baseline has no such ambiguity: it is uniform across tools
and callers, so cross-role agreement is a plain equality check with no fail-open. Per-tool
(and genuinely per-caller) governance waits for v2, where a *per-role* stance in the bundle
can represent it without compression.

Deferring the closed-world check matters because the policy and the manifest are
authored and resolved at different times. A policy is valid on its own; the set of
guards an agent actually declares is only known once that agent is registered. If
parse-time rejected a guard name not yet in a manifest, a policy written ahead of
(or independently from) its agent would fail to load, and inheritance/module
composition — which resolve policies with no agent in hand — could not run at all.
The stop-cold guarantee is not weakened, only relocated to the seam that has both
halves: PR3 rejects an enable/disable that names an undeclared guard.

## Consequences

- PR3 (R-GUARD-007) reads `effective_guards` off the signed bundle at pipeline
  build, filters the guard list, and applies the closed-world "stop cold" check
  against the manifest.
- Analyzer lints for the block (a guard rule that no manifest guard matches, a
  redundant override) are deferred to PR4.
- `BaseToolPolicy` carries **no** `guards` field, so a `tools:` / `default_policy` /
  `admission:` / `agents:` / `skills:` entry cannot hold one. A `BaseToolPolicy`
  before-validator rejects a `guards:` on any of them with a message that names the fix
  (move it to the baseline), rather than the raw `extra="forbid"` error — one validator
  covering every nested scope.
- The baseline `guards` map (on `AgentPolicy`) merges per key across inheritance, like
  `tools` (a child or later mixin re-declaring a guard's rule overrides the parent's).
- No forced recompile: `guards` is not module-composable, so the resolved (modular)
  serializer drops the always-empty `guards` maps, leaving a guards-free project's
  resolved YAML byte-identical to before the block existed. A stored modular
  bundle's `source_hash` does not move, so the AI Act report reads no false
  `MATRIX_SOURCE_DRIFTED`. (Contrast a single-file policy that *authors* a `guards:`
  block: its source hash moves because the operator genuinely edited it.)

## Rejected alternatives

- **Lower guards into `effective_tools` as synthetic keys** (like `agent.*` /
  `skill*:`). Wrong semantics: a build-time toggle becomes a per-call allow/deny
  decision, gains engine latency, and adds non-deciding keys to the closed-world
  tool namespace and its Rego exclusion set.
- **A bare bool value** (`guards: {secret_guard: false}`). Turned down for a
  `{enabled: bool}` model, so v2 can add fields without breaking the grammar and so
  `extra="forbid"` catches a typo'd key rather than silently dropping it.
- **Per-tool overrides in v1** (a `guards:` inside a `tools:` entry). Turned down: the
  single agent-level stance cannot represent them per-caller, and folding them across
  roles is a fail-open trap (see Why). Deferred to v2 with a per-role bundle stance.
- **Enforce closed-world at parse time.** Turned down: parse has no manifest, and
  it would break policies authored before their agent, plus inheritance/module
  resolution that run with no agent in hand.

## Verify

```
pytest tests/security/test_guard_policy.py
```
