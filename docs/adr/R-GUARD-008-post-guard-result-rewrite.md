# R-GUARD-008: Post-guards rewrite a result by replacing it, not mutating it

**Status:** Accepted · 2026-10-01
**Applies to:** `hexgate/guards/**`
**Supersedes:** [R-GUARD-003](R-GUARD-003-post-guard-result-rewrite-deferred.md)

## Decision

A post-tool guard MAY rewrite a tool's result by returning `Proceed(result=...)`. The runner replaces the outcome's `value` wholesale with the object the guard returns and hands that to the next post-guard and back to the caller. The rewrite is position- and state-bound: the runner rejects `Proceed(args=...)` from a post-guard (the tool has already run, so there is nothing to rewrite) and rejects `Proceed(result=...)` on a failed outcome (`ok=False` — there is no result to replace). A plain `Proceed()` stays a no-op, and observe guards still cannot rewrite or halt.

## Why

R-GUARD-003 deferred result rewrite because a tool result is an arbitrary Python object (a pydantic model, a dataframe, a bare string), and the runner had no well-defined way to redact one *in place* — the only safe generic target is a serialized projection, and v1 did not build that rule.

The projection rule turned out to be unnecessary: the runner never needs to walk or mutate the result itself. The guard author already holds the result and knows its shape, so they produce a complete replacement object and the runner swaps it in functionally (`dataclasses.replace(outcome, value=...)`). A JSON-ish payload is cleaned by walking a copy (`redact_secrets` returns a new structure); an opaque object is the guard's own call. Responsibility for "what a safe rewrite of *this* result looks like" sits with the guard that understands the type, not with a generic rule in the runner. That is the same split R-GUARD-003 already used for args — `Proceed(args=...)` carries a fully-formed replacement — applied to the result side.

In-place mutation stays forbidden, exactly as R-GUARD-003 enforced it: when post-guards exist, a dict result is handed to them wrapped in a read-only `MappingProxyType`, so a guard that tries to mutate the object in place raises rather than leaking a change past the provenance tier. Rewrite is therefore the *only* channel that changes a result, and it is explicit and recorded. When no guard rewrites, the caller gets back the original unsealed object (`raw`), so the seal costs nothing on the pass-through path.

## Consequences

- A result-scanning plugin ships as a scrubber, not merely a watcher: `secret_scrubber` returns `Proceed(result=cleaned)` from `redact_secrets`, so the cleaned result is what reaches the model and the user. `secret_watch` remains for the observe-only case (flag, do not change).
- Every rewrite records a `Modification(target="result", ...)` — count and categories, never the value — so the audit trail shows the result was changed and by which guard, mirroring the args-rewrite trail.
- A post-guard still runs when the tool raised (`ToolOutcome(ok=False, error=...)`), but may only observe or halt there; `Proceed(result=...)` on a failed call is a programming error and raises.
- The `Proceed.result` `_UNSET` sentinel reserved by R-GUARD-003 is now the live rewrite channel, so lifting the deferral was an implementation change, not a contract break.

## Rejected alternatives

- **A generic in-runner projection rule** (walk JSON-ish in place, flag opaque objects). Unneeded once the guard returns a complete replacement; it would also re-impose a mutation model on types the runner cannot understand.
- **Allow in-place mutation of the sealed result.** Would route a change around the `Proceed`/`Modification` provenance tier; the read-only seal deliberately prevents it.

## Verify

```
pytest tests/guards/test_runner.py -k post_guard_result_rewrite
pytest tests/plugins/test_guards.py -k secret_scrubber
```

A post-guard returning `Proceed(result=...)` replaces the value and chains to the next post-guard; the same on a failed call raises; `secret_scrubber` strips a credential from a result end-to-end.
