# R-POL-003: Run every policy check through `analyze_policy`

**Status:** Accepted · 2026-10-05
**Applies to:** `hexgate/security/analyzer.py`, `hexgate/cli/policy/**`, `platform/api/hexgate_api/features/policy_modules/service.py`, `platform/api/hexgate_api/features/policy_modules/router.py`, `platform/api/hexgate_api/features/agents/router.py`, `platform/dashboard/src/routes/Policies.tsx`, `evals/policy_writing/**`

## Decision

Get every entry point's policy lints from one SDK function per input form: `hexgate.security.analyze_policy(policy_set, *, manifest=None, source=None)` for a resolved `PolicySet`, and `check_project` for a module store.

- An entry point (the CLI's `policy validate` / `check`, the platform's `/policy/preview`, `/policy/check` and agent `/validate`, the policy-writing eval scorer) MUST only gather inputs (resolve the `PolicySet`, load the manifest), call that function, and present what it returns.
- An entry point MUST NOT call an individual check (`lint_guards`, `check_default_role_exposure`, `_resolved_drift`, a bare `PolicySet.guard_stance()`, ...) or keep its own list of them; a new check goes inside `analyze_policy`.
- Manifest-dependent checks MUST run only when a manifest is passed. An entry point that has the agent's manifest MUST pass it, except the agent `policy_yaml` save, which passes none (see the last bullet).
- An entry point MAY de-duplicate, filter by role or agent, apply a severity gate to what it reports (never to a save; see below), or route lints by code, but MUST NOT discard a check's lints unconditionally.
- Severity MUST NOT decide whether a save is blocked. On the agent `policy_yaml` save and its `/validate` (`agents/router.py`), a lint is an error (a 422 on save, `ok: false` on `/validate`) only if its code is in `_BLOCKING_FINDINGS`, and a code goes there only if it is manifest-free and means the SDK cannot load the policy (today only `guard-divergence`). Every other lint, `error` severity included, is a warning that still reaches the client with its severity, and `/policy/check`'s `ok` describes the lints, not whether the policy saves. Module and policy-file saves (`policy_modules/router.py`) block on resolution failure instead, which this rule does not govern. Likewise the dashboard MUST treat only `link-error`, not any `error` lint, as "the policy doesn't compose".

## Why

The checks were implemented once, in the SDK, but each entry point picked its own subset of them (#303). The code never diverged; the *coverage* did:

- `unknown-guard` ran only in `hexgate policy validate --manifest`. On the platform, a typo like `secret_redacter: {enabled: false}` saved silently and changed nothing.
- The cross-role guard check ran in `/validate` and on save, but not in the CLI.
- `/policy/check` passed no manifest, so unknown tools and arguments were never flagged on the platform.

Every new check repeated the drift: it was written in the SDK and then wired into some entry points and not others. Nothing failed when an entry point was skipped, so the gap only showed up when a user hit it. With one function, the decision of *which* checks run moves from N call sites to one place.

`check_project` stays a separate function by design, not as debt. Linking drops a boundary fence on a misspelled tool, so the resolved `PolicySet` leaves the real tool uncapped with nothing left to flag; only the module form still sees that drift. Folding the module-store branch into `analyze_policy` would silently lose that check.

Blocking is kept apart from severity because a save asks two questions. *Can the SDK load this?* `guard-divergence` says no: v1's guard stance applies to the whole agent (R-GUARD-007), so the SDK raises at construction whatever the manifest says. *Does it match the agent's code right now?* `unknown-guard` and the drift lints answer that against the latest registered manifest, which lags each deploy. Naming a guard and then deploying the code that adds it is the normal order, so blocking on those would refuse correct policies. They stay `error` severity anyway, because with that manifest the policy is wrong at runtime: the agent stops on `unknown-guard`, and an `error`-graded drift lint leaves the real tool less restricted than the policy says, with no error to show it. Keeping blocking codes manifest-free is also what makes `/validate` (which passes the manifest) and the save route (which passes none) agree.

## Consequences

- The platform's compose routes call `analyze_policy` once per agent: every declared agent and every registered agent, each with its own manifest, plus the generic `"*"` view without one (`_compose_lints`).
- Guard divergence moved from the platform's `_load_document` (which forced `guard_stance()`) into `analyze_policy`, so every entry point reports it instead of only the save route.
- `hexgate policy validate` now fails on `guard-divergence`, and with `--manifest` on an `error`-severity `unknown-tool` / `unknown-arg`, at its default `--max-severity error`. A CI job that passes `--manifest` can start failing on drift it never saw before; that is the intended effect, since an `error` drift lint means the real tool runs looser than the policy says.
- `analyze_policy` also runs the manifest-free `unknown-root` (a constraint path no call sets), so `hexgate policy validate` without `--manifest` fails on one graded `error`: under an odd number of `not`, where the fence is always true. On the platform it stays a warning like every other non-blocking code.
- Until it is routed (#303 follow-up), the eval scorer (#287) still violates this rule. The Verify grep covers `evals/` once #287 lands.
- Two known gaps remain. A compose `policy.yaml` is module-built (each `boundary` lowers to a boundary module), but its routes lint the resolved `PolicySet`, so a boundary fence on a misspelled tool is not flagged there. And the tier (module-store) branch of `/policy/check` calls `check_project` without a manifest, so it reports no `unknown-tool` / `unknown-arg`.

## Rejected alternatives

- **A shared registry of checks that each entry point iterates.** It keeps N loops over the registry, and each loop can still skip or reorder entries; one function call leaves nothing to get wrong at the call site.
- **A `project=` input alongside the policy.** Every single-agent shape (a classic single-file policy, each agent of a compose `policy.yaml`) already resolves to a `PolicySet`, so a second input would only re-encode the gathering step that differs per caller.
- **Block on any `error`-severity lint, or downgrade the manifest lints to warnings.** The first refuses a guard named ahead of its deploy; the second hides a guard typo the agent stops on, and drift that silently leaves a tool uncapped.
- **Requiring a manifest.** A policy is checked before its agent registers (R-GUARD-006), so the manifest-free checks must still run without one.

## Verify

```
grep -rsnE "\b(lint_guards|check_default_role_exposure|_resolved_drift|guard_stance)\(" hexgate platform/api/hexgate_api evals --include='*.py' | grep -v "hexgate/security/"
```

It must print nothing. `evals/` does not exist until #287 lands, and `-s` keeps the grep quiet about it until then.

```
cd platform/api && uv run --python 3.13 pytest -q \
  "tests/features/policy_modules/test_policy_modules.py::test_when_a_policy_is_bad_then_every_entry_point_reports_the_sdk_findings" \
  "tests/features/agents/test_agents.py::test_validate_reports_divergent_guard_stance" \
  "tests/features/policy_modules/test_policy_modules.py::test_when_validate_finds_a_guard_typo_then_it_warns_without_failing"
```

That runs the parity test: one known-bad policy through `/validate`, `/policy/preview` and `/policy/check`. Each must report exactly the codes `analyze_policy` returns, so a check added to the function but bypassed by a route fails it. The other two check that divergence fails `/validate`, and that a guard typo warns with `severity: error` and `ok: true`.

```
uv run --python 3.13 pytest -q tests/cli/test_policy.py \
  -k "test_when_roles_disagree_on_guards_then_validate_reports_guard_divergence or test_when_manifest_lacks_a_tool_or_arg_then_validate_reports_drift"
```

That covers the CLI: `policy validate` reports `guard-divergence` without a manifest, and the drift lints only with `--manifest`.
