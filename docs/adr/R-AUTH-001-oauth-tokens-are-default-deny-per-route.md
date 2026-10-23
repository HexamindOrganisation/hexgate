# R-AUTH-001: Accept an OAuth token only on a route that declares its scope

**Status:** Accepted · 2026-10-23
**Applies to:** `platform/api/hexgate_api/deps/identity.py`, `platform/api/hexgate_api/constants.py`, `platform/api/hexgate_api/deps/org.py`, `platform/api/hexgate_api/deps/project.py`, `platform/api/hexgate_api/features/**/router.py`, `platform/api/hexgate_api/features/projects/service.py`, `platform/api/tests/features/oauth/test_route_marks.py`, `platform/api/tests/features/oauth/test_project_claim.py`

## Decision

An OAuth access token (an MCP client acting as a user) is default-deny: a route accepts one only when it declares the scope that covers it, and the API checks that scope and the token's projects itself, in `require_user`.

- A route that accepts an OAuth token MUST declare `openapi_extra={OAUTH_SCOPE_KEY: <scope>}`, where the value is one scope of `OAUTH_SCOPES` or a non-empty list of them (all required).
- The set of marked routes MUST equal the MCP spec's §I scope registry, and `REGISTRY` in `test_route_marks.py` MUST list exactly that set. Marking a new route is a change to the registry, not a one-line edit.
- A route behind `require_user` with no mark MUST refuse every OAuth token, whatever its scopes (403, `insufficient_scope`). A route that does not chain through `require_user` (SDK, OTLP, public routes) MUST answer a token exactly as it answers no auth.
- `require_user` MUST check the scope and the `projects` claim on every OAuth call. It MUST NOT trust that the MCP server already checked them.
- A marked route MUST either have a `project_id` path parameter, checked against the token's `projects` claim, or carry `OAUTH_NO_PROJECT: True`. Only `GET /orgs` and `GET /orgs/{org_id}/projects` carry the tag, and for an OAuth caller they MUST filter their results to the projects the token names.
- The scope and project checks narrow a grant; they MUST NOT replace the route's own permission check (`require_org_member`, `require_project_admin`), which still runs after.

## Why

`require_user` is the one dependency every dashboard route chains through, so "a valid token is a logged-in user" would hand an AI client every cookie route at once: invitations, member removal, org settings, API key minting, the agent `policy_yaml` save that only needs org membership. A route gets that access by doing nothing, so the safe default is the opposite one: a route that says nothing accepts no token, and the only way in is a mark someone wrote on purpose.

A wrong or extra mark fails silently. A cookie-only route that picks up a mark becomes reachable by an AI client, and nothing logs a refusal that never happens. That is why the marked set is pinned to the registry in a test: widening it shows up as a diff to `REGISTRY` that a reviewer reads, and the same test calls every unmarked route behind `require_user` with an all-scopes token for the default project and asserts the refusal (every other route must answer the token exactly as it answers no auth), so a route added later without a mark is covered without anyone remembering to add it.

The API checks the scope itself because the MCP server is not the only client: anyone holding the token can call the API directly with it. The MCP server's check gives a clean error message; the API's check is the security boundary and must be complete on its own.

The project check sits in `require_user`, beside the scope, so no handler can forget it. A missing `project_id` is a refusal rather than a skip: if absence meant "nothing to check", any later route without the parameter would inherit an exemption by accident. The two org routes are the deliberate exception, since finding a `project_id` is what they are for, and they filter by the claim instead so `projects:read` cannot list every org's inventory.

## Consequences

- Every new route an MCP tool needs (the policy endpoints of plan PRs 19–23) adds a mark and a `REGISTRY` entry in the same PR.
- `GET /orgs` skips the personal-org repair for an OAuth caller: the repair is a write, and a read scope must not cause one.
- Websocket routes are not enumerated: they authenticate with their own cookie-only `ws_require_*` dependencies and never call `require_user`.

## Rejected alternatives

- **Accept any valid token on any route `require_user` guards, and rely on scopes at the MCP server.** The API would then trust a check a direct caller skips.
- **An allow-list of route paths inside `require_user`.** It would drift from the routes it names; a mark on the route itself moves with it, and the test pins the set.

## Verify

```
cd platform/api && uv run pytest -q tests/features/oauth/test_route_marks.py tests/features/oauth/test_project_claim.py
```
