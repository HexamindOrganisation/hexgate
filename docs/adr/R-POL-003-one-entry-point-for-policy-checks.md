# R-POL-003: Run every policy check through `analyze_policy`

**Status:** Accepted · 2026-10-05
**Applies to:** `hexgate/security/analyzer.py`, `hexgate/cli/policy/**`, `platform/api/hexgate_api/features/policy_modules/service.py`, `platform/api/hexgate_api/features/policy_modules/router.py`, `platform/api/hexgate_api/features/agents/router.py`, `evals/policy_writing/**`

## Decision

Every entry point that reports policy problems gets its lints from one SDK function per input form: `hexgate.security.analyze_policy(policy_set, *, manifest=None, source=None)` for a resolved `PolicySet`, and `check_project` for a module store.

- An entry point (the CLI's `policy validate` / `check`, the platform's `/policy/preview`, `/policy/check` and agent `/validate`, the policy-writing eval scorer) MUST only gather inputs (resolve the `PolicySet`, load the manifest), call that function, and present what it returns.
- An entry point MUST NOT call an individual check (`lint_guards`, `check_default_role_exposure`, `_resolved_drift`, a bare `PolicySet.guard_stance()`, ...) or keep its own list of them; a new check goes inside `analyze_policy`.
- Manifest-dependent checks MUST run only when a manifest is passed. An entry point that has the agent's manifest MUST pass it, except the save route (R-POL-004).
- An entry point MAY de-duplicate, filter by role or agent, apply a severity gate, or route lints by code (R-POL-004), but MUST NOT discard a check's lints unconditionally.

## Why

The checks were implemented once, in the SDK, but each entry point picked its own subset of them (#303). The code never diverged; the *coverage* did:

- `unknown-guard` ran only in `hexgate policy validate --manifest`. On the platform, a typo like `secret_redacter: {enabled: false}` saved silently and changed nothing.
- The cross-role guard check ran in `/validate` and on save, but not in the CLI.
- `/policy/check` passed no manifest, so unknown tools and arguments were never flagged on the platform.

Every new check repeated the drift: it was written in the SDK and then wired into some entry points and not others. Nothing failed when an entry point was skipped, so the gap only showed up when a user hit it. With one function, the decision of *which* checks run moves from N call sites to one place.

`check_project` stays a separate function by design, not as debt. Linking drops a boundary fence on a misspelled tool, so the resolved `PolicySet` leaves the real tool uncapped with nothing left to flag; only the module form still sees that drift. Folding the module-store branch into `analyze_policy` would silently lose that check.

## Consequences

- The platform's compose routes call `analyze_policy` once per agent: every declared agent and every registered agent, each with its own manifest, plus the generic `"*"` view without one (`_compose_lints`).
- Guard divergence moved from the platform's `_load_document` (which forced `guard_stance()`) into `analyze_policy`, so every entry point reports it instead of only the save route.
- Until they are routed (#303 follow-ups), the CLI (`hexgate/cli/policy/main.py`) and the eval scorer (#287) still violate this rule. The Verify grep lists the CLI, and covers `evals/` once #287 lands.
- Two known gaps remain. A compose `policy.yaml` is module-built (each `boundary` lowers to a boundary module), but its routes lint the resolved `PolicySet`, so a boundary fence on a misspelled tool is not flagged there. And the tier (module-store) branch of `/policy/check` calls `check_project` without a manifest, so it reports no `unknown-tool` / `unknown-arg`.

## Rejected alternatives

- **A shared registry of checks that each entry point iterates.** It keeps N loops over the registry, and each loop can still skip or reorder entries; one function call leaves nothing to get wrong at the call site.
- **A `project=` input alongside the policy.** Every single-agent shape (a classic single-file policy, each agent of a compose `policy.yaml`) already resolves to a `PolicySet`, so a second input would only re-encode the gathering step that differs per caller.
- **Requiring a manifest.** A policy is checked before its agent registers (R-GUARD-006), so the manifest-free checks must still run without one.

## Verify

```
grep -rsnE "\b(lint_guards|check_default_role_exposure|_resolved_drift|guard_stance)\(" hexgate platform/api/hexgate_api evals --include='*.py' | grep -v "hexgate/security/"
```

It must print nothing once the CLI and eval follow-ups land (today it lists `hexgate/cli/policy/main.py`).

```
cd platform/api && uv run --python 3.13 pytest -q "tests/features/policy_modules/test_policy_modules.py::test_when_a_policy_is_bad_then_every_entry_point_reports_the_sdk_findings"
```

That runs the parity test: one known-bad policy through `/validate`, `/policy/preview` and `/policy/check`. Each must report exactly the codes `analyze_policy` returns, so a check added to the function but bypassed by a route fails it.
