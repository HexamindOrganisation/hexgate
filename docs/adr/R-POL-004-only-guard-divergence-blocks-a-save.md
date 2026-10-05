# R-POL-004: Only guard divergence blocks a policy save

**Status:** Accepted · 2026-10-05
**Applies to:** `platform/api/hexgate_api/features/agents/router.py`, `platform/api/hexgate_api/features/policy_modules/service.py`, `platform/api/hexgate_api/features/policy_modules/router.py`, `platform/dashboard/src/routes/Policies.tsx`

## Decision

Of the lints `analyze_policy` returns (R-POL-003), only those meaning "the SDK cannot load this policy" block a save; today that is only `guard-divergence`. Every other lint is advisory, whatever its severity.

- The single-document save and `/validate` MUST put a lint in `errors` (a 422) only if its code is in `_BLOCKING_FINDINGS` (`{"guard-divergence"}`); every other lint goes to `warnings`, including error-severity ones like `unknown-guard`.
- A code MUST be added to `_BLOCKING_FINDINGS` only if it is manifest-free and means the SDK cannot load the policy, so `/validate` (which passes the latest registered manifest) and the save route (which passes none) give the same verdict.
- A lint's `severity` MUST reach the client unchanged (the `severity` field on `/validate` diagnostics).
- A route or client MUST NOT derive "the policy doesn't compose" from lint severity. Only a load or link failure means that; in the dashboard, that is the platform's `link-error` lint.

## Why

Two different questions meet at a save: *can the SDK load this policy at all*, and *does this policy match the agent's code right now*.

`guard-divergence` is the first kind. Roles that resolve to different guard settings cannot be represented, because v1's guard stance is agent-level: one stance governs every caller (R-GUARD-006, R-GUARD-007). The SDK raises on such a policy at construction, whatever the manifest says. Storing it would hand the agent a policy that is guaranteed to stop it cold, so the save is refused. If a per-role stance lands (v2), this lint stops meaning "unloadable" and leaves `_BLOCKING_FINDINGS`.

The manifest-dependent lints (`unknown-guard`, `ambiguous-guard`, `unknown-tool`, `unknown-arg`) are the second kind. A policy is legitimately written ahead of its agent (R-GUARD-006), and on the platform these lints compare it with the agent's *latest registered* manifest, a snapshot that lags each deploy. The normal workflow is to name a guard in the policy and then deploy the code that adds it. Blocking the save would force the opposite order: ship code governed by no policy, then write the policy. A false "unknown" from a stale manifest would block a correct policy, and the author could not fix it from the editor. The same lag is why a blocking code must be manifest-free: otherwise `/validate` and save would disagree.

Severity and blocking are kept apart on purpose. `unknown-guard` is `error` severity because the runtime would stop on it *if the agent built with that manifest*; reporting it as a warning would hide that. But blocking follows from whether the *document* is loadable, not from how bad a lint is.

## Consequences

- `/validate` and the save route share `_validate_policy_document`, so they agree on what blocks: a policy that validates is one that saves.
- `/policy/check` and `/policy/preview` never gate a save. `/policy/check` can return `ok: false` for an error-severity advisory lint while the same policy saves fine; `ok` describes the lints, not saveability.

## Rejected alternatives

- **Block on any error-severity lint.** Manifest lints lag deploys, so a correct policy would be unsavable (see Why).
- **Downgrade manifest lints to warning severity.** It hides that the runtime would stop on them (see Why).

## Verify

```
cd platform/api && uv run --python 3.13 pytest -q \
  "tests/features/agents/test_agents.py::test_validate_reports_divergent_guard_stance" \
  "tests/features/policy_modules/test_policy_modules.py::test_when_validate_finds_a_guard_typo_then_it_warns_without_failing"
```

The first checks that divergence fails `/validate`. The second checks that a guard typo warns with `severity: error` and `ok: true`.
