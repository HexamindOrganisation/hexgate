# Deploying Hexgate (prod + staging on one machine)

Two environments share one box. Each is an isolated Compose **project**
(`hexgate-prod`, `hexgate-staging`) built from the same
[`docker-compose.deploy.yml`](docker-compose.deploy.yml) — its own Postgres,
ClickHouse, Redpanda, keystore, and network. Each env's **api** serves both the
JSON API (`/v1/*`) and that env's dashboard SPA same-origin, listening plain
HTTP on a **loopback port** (prod `7000`, staging `7200`). Its **collector**
(OTLP/HTTP receiver for the SDK's spans) listens on a second loopback port
(prod `7001`, staging `7201`), and its **MCP server** (what Claude Code and
other MCP clients connect to) on a third (prod `7002`, staging `7202`). There
is no separate edge container — the SPA
is baked into the API image and served by it.

The box **already runs a reverse proxy** that owns 80/443 and terminates TLS.
Hexgate does not ship its own front proxy — you point the existing one at the
three loopback ports by hostname and path.

```
                            one machine (one IP)
 Internet ──:443──▶  the box's existing reverse proxy — TLS
   app.hexgate.ai          │  Host: app.hexgate.ai          → 127.0.0.1:7000
                           │    path /v1/traces             → 127.0.0.1:7001
                           │    path /mcp (+ its metadata)  → 127.0.0.1:7002
   app.staging.hexgate.ai  │  Host: app.staging.hexgate.ai  → 127.0.0.1:7200
                           │    path /v1/traces             → 127.0.0.1:7201
                           │    path /mcp (+ its metadata)  → 127.0.0.1:7202
                  ┌───────┴───────────────┐    ┌───────────────────────┐
                  │ hexgate-prod          │    │ hexgate-staging       │
                  │  api→pg,ch            │    │  api→pg,ch            │
                  │  collector→redpanda   │    │  collector→redpanda   │
                  │  enricher: redpanda→ch│    │  enricher: redpanda→ch│
                  │  mcp→api              │    │  mcp→api              │
                  └───────────────────────┘    └───────────────────────┘
```

The reverse proxy must be able to reach the host loopback (it's host-installed
or runs with host networking). A bridged proxy container can't see
`127.0.0.1:7000` — in that case change the `api`, `collector` AND `mcp` port
bindings in the compose to publish on a shared Docker network or a
non-loopback port instead (republishing only the api leaves every
`/v1/traces` POST and every MCP call 502ing while `/v1/*` works).

Images build **on the box** — no registry. amd64 throughout (the API bundles
the amd64-only OPA binary; the image also runs a Node build stage to bundle the
dashboard SPA).

## 1. DNS

Point both hostnames at the box's IP **before** first start (the reverse
proxy's TLS issuance checks them):

```
app.hexgate.ai          A   <box-IP>
app.staging.hexgate.ai  A   <box-IP>
```

**80 and 443** are owned by the box's reverse proxy. The 7000/7200 (api),
7001/7201 (collector) and 7002/7202 (mcp) ports stay loopback — never exposed.

## 2. Layout (two checkouts)

So staging can run ahead of prod, check each environment out separately:

```bash
git clone <repo> /srv/hexgate-prod    && cd /srv/hexgate-prod    && git checkout <release-tag>
git clone <repo> /srv/hexgate-staging && cd /srv/hexgate-staging && git checkout main
```

Project names are fixed in the Makefile, so isolation holds regardless of
directory.

## 3. Configure

`platform/.env.<stage>` is **pulled from the Scaleway secret `/hexgate/<stage>`**
(opaque, full env file as payload), never hand-copied.

**Admin, one-time:** create `/hexgate/<stage>` as an opaque secret in `fr-par`
from the `platform/.env.sample` template — `HEXGATE_POSTGRES_PASSWORD`,
`HEXGATE_CLICKHOUSE_PASSWORD` (`openssl rand -hex 32`), any `RESEND_API_KEY` /
Google OAuth values. Editing it adds a version; pull takes the latest.

**Box prerequisites:** `scw` + `jq`, with read-only Secret Manager creds
(`scw init`, or `SCW_ACCESS_KEY` / `SCW_SECRET_KEY` / `SCW_DEFAULT_PROJECT_ID`).
Region defaults to `fr-par` (`SCW_DEFAULT_REGION`), folder to `/hexgate`
(`HEXGATE_SECRET_PATH`).

```bash
make platform-env-pull STAGE=prod      # writes platform/.env.prod (also auto-run by platform-up if absent)
```

Then add two routes to the box's reverse proxy (one-time), so each hostname
terminates TLS and proxies to its loopback port:

```
app.hexgate.ai          → 127.0.0.1:7000   (except path /v1/traces → 127.0.0.1:7001, and the MCP paths → 127.0.0.1:7002)
app.staging.hexgate.ai  → 127.0.0.1:7200   (except path /v1/traces → 127.0.0.1:7201, and the MCP paths → 127.0.0.1:7202)
```

The `/v1/traces` path routes to the stage's `HEXGATE_OTLP_PORT` — the
collector's OTLP/HTTP receiver — and must match BEFORE the hostname's
catch-all route (in Caddy a `handle /v1/traces` block above the general
`reverse_proxy`; in nginx an exact `location = /v1/traces`). This is what
makes the SDK's default OTLP endpoint (`<api url>/v1/traces`) work with zero
client config. Without the route, the api's SPA catch-all answers the POST
with a JSON 404 and every SDK on that stage silently loses its audit trail —
`make platform-smoke` (§4) is how you catch that. Everything else in the
stack (Postgres, ClickHouse, Redpanda) binds no host ports; Redpanda in
particular is PLAINTEXT with no auth — never publish it.

**The `/v1/traces` route carries its own request-body limit.** The collector's
OTLP receiver accepts 32 MiB (`max_request_body_size`) and an SDK export of LLM
message spans can reach ~17 MiB, but the proxy enforces its own cap *first* and
answers `413` before the collector ever sees the POST — so the proxy, not the
collector, is what decides whether a large export gets through. This is the one
hop in the record-size budget (`docs/internals/audit-pipeline.md` §4.1) that
lives outside this repo, so nothing in `make check-all` can catch it.

In nginx the setting is `client_max_body_size`, and it belongs **inside the
`location = /v1/traces` block** rather than at `http` or `server` level:

```nginx
location = /v1/traces {
    proxy_pass http://127.0.0.1:7001;   # 7201 on staging
    client_max_body_size 32m;
}
```

Note the value here is deliberately *tighter* than the surrounding default —
on the prod box the `http` block allows `100M`, and this route is held to a
much smaller number on purpose. `/v1/traces` is the only endpoint taking bulk
binary uploads, and nginx enforces the limit **before** the collector verifies
the Biscuit token, so whatever it allows is what an unauthenticated caller can
make the proxy buffer. Keep it as small as the largest legitimate export: 32
MiB covers a 64-span batch of maximally-sized message spans, and nothing more.
The value was 8 MiB before message logging, which is why it had to be raised.

Both stages need it — they are separate files
(`sites-available/app.hexgate.ai` and `…/app.staging.hexgate.ai`), each with
its own `/v1/traces` block pointing at that stage's collector port. Apply with
`nginx -t && systemctl reload nginx`; a graceful reload keeps existing
connections and a failed test changes nothing. Caddy imposes no default body
limit and needs nothing.

(In Caddy this is two `reverse_proxy` site blocks; in nginx, two `server`
blocks with `proxy_pass`. Forward `X-Forwarded-Proto: https` — the API trusts
it, via uvicorn `--proxy-headers`, for correct https OAuth callbacks — and, in
nginx, `proxy_set_header Host $host` (Caddy passes the client's Host already).
`/v1` includes WebSocket endpoints, so the proxy must pass upgrades — Caddy
does this automatically; nginx needs the `Upgrade`/`Connection` headers set.)

**The MCP routes.** Two paths on the api hostname go to the stage's
`HEXGATE_MCP_PORT` instead of the hostname's catch-all:

- `/mcp`, as a **prefix** (nginx `location ^~ /mcp`, not `location = /mcp`;
  Caddy `handle /mcp*`): the MCP server redirects `/mcp/` to `/mcp`, and an exact
  match would hand `/mcp/` to the api instead.
- `/.well-known/oauth-protected-resource/mcp`, the MCP server's own metadata.
  This is the document an MCP client reads first, after the 401 from `/mcp`;
  it names the api as the authorization server.

The other two discovery documents, `/.well-known/oauth-authorization-server`
and `/.well-known/jwks.json`, are the api's and stay on the catch-all. The MCP
server fetches the JWKS from `http://api:8000` on the compose network, never
through the proxy. Without either route the api answers in the MCP server's
place (the metadata path with a JSON 404), and the client's connect or
discovery fails. `HEXGATE_PUBLIC_URL` and `HEXGATE_MCP_RESOURCE_URL` (section
6) must name this same hostname: a client that
connects to `https://app.hexgate.ai/mcp` is only given a token for the
resource URL the MCP server advertises.

**Rate-limit the unauthenticated OAuth and MCP paths.** The api has no request
rate limiting of its own, and the `/.well-known/` discovery documents, `/mcp`
and the OAuth endpoints under `/v1/oauth/` answer callers that carry no
credentials, by design: discovery, the login redirect and the token exchange
come before any token exists. `/v1/oauth/authorize` and `/v1/oauth/token` also
make the api fetch the client's metadata document from the host the caller
names (on a cache miss, which a new hostname always is), so an unthrottled
caller can keep the api's DNS lookups and outbound fetches busy with one
distinct hostname per request. Limit each of these paths per client IP at the
proxy. In nginx, a zone at `http` level (e.g.
`/etc/nginx/conf.d/hexgate-ratelimit.conf`, once for both stages):

```nginx
limit_req_zone $binary_remote_addr zone=hexgate_oauth:10m rate=5r/s;
limit_req_zone $binary_remote_addr zone=hexgate_mcp:10m rate=20r/s;
```

then, in each stage's `server` block (ports shown for prod; on staging use 7202
for 7002 and 7200 for 7000, or staging's MCP and OAuth traffic reaches prod).
`limit_req_status` goes here rather than at `http` level, where it would also
change the status of any other site's limits on the box:

```nginx
limit_req_status 429;
location ^~ /mcp {
    limit_req zone=hexgate_mcp burst=40 nodelay;
    client_max_body_size 1m;
    proxy_pass http://127.0.0.1:7002;
    proxy_buffering off;    # MCP replies may stream as server-sent events
}
location = /.well-known/oauth-protected-resource/mcp {
    limit_req zone=hexgate_oauth burst=20 nodelay;
    proxy_pass http://127.0.0.1:7002;
}
location = /.well-known/oauth-authorization-server {
    limit_req zone=hexgate_oauth burst=20 nodelay;
    proxy_pass http://127.0.0.1:7000;
}
location = /.well-known/jwks.json {
    limit_req zone=hexgate_oauth burst=20 nodelay;
    proxy_pass http://127.0.0.1:7000;
}
location ^~ /v1/oauth/ {
    limit_req zone=hexgate_oauth burst=20 nodelay;
    client_max_body_size 1m;
    proxy_pass http://127.0.0.1:7000;
}
```

nginx takes an exact `=` match first, then the longest prefix, and `^~` stops
it from trying regex locations after that, so these blocks win over `location
/` and over any `location ~ …` the server already has (a common `location ~ /\.
{ deny all; }` would otherwise refuse the discovery documents) wherever they
sit in the file. The three `/.well-known/` routes are exact matches, the only
such paths the api and the MCP server serve, so any other `/.well-known/`
handling the server already has (an ACME challenge, a `security.txt`) keeps
working. The `/v1/oauth/` one also covers the two calls the dashboard's consent
page makes (`GET /v1/oauth/consent-info` and the consent `POST
/v1/oauth/authorize`); a login makes each once, far under the limit. The 1 MiB
body cap holds `/mcp` and `/v1/oauth/` to what they carry (a JSON-RPC call with
at most a policy draft, the consent JSON, the token form) rather than the prod
box's `http`-level 100M. Put every `proxy_set_header` at `server` level and
none inside a `location`, this server's `location /` included: a `location`
that sets any header of its own inherits none of the `server`'s. That covers
`X-Forwarded-Proto`, `Host $host` and the WebSocket `Upgrade`/`Connection` pair
(above). For `/mcp`, `Host $host` matters: the MCP server's `/mcp/` → `/mcp`
redirect is built from the Host it receives (uvicorn's proxy headers correct
the scheme, not the host), so without it the redirect points at
`127.0.0.1:7002`.

A login is about eight requests on these paths (the first `/mcp` call's 401,
the two metadata documents, `/authorize`, the consent page's two `/v1/oauth`
calls, `/token`, the retried `/mcp` call), well inside the burst; each tool
call after that is one `/mcp` request. A caller over the limit gets `429`.
`$binary_remote_addr` is the client's address only because nothing sits in
front of this proxy; put a CDN or load balancer in front of it and the key
becomes that hop's address, throttling every user as one. Caddy has no rate
limiter built in: it needs the `caddy-ratelimit` module (`xcaddy build --with
github.com/mholt/caddy-ratelimit`) and its `rate_limit` directive, keyed on
`{remote_host}`, in the same five routes; being a third-party handler, it also
needs `order rate_limit before reverse_proxy` in the global options (or each
route wrapped in `route { }`).

## 4. Launch — env stacks, then wire up the proxy

```bash
# /srv/hexgate-prod
make platform-up STAGE=prod
# /srv/hexgate-staging
make platform-up STAGE=staging
# then reload the box's reverse proxy so it routes to 7000/7200 (+ /v1/traces → 7001/7201, MCP paths → 7002/7202)
```

Bring the env stacks up before the proxy routes them, so TLS issuance doesn't
race the upstreams. First boot per env: ClickHouse runs `schema.sql` once,
`redpanda-init` creates the two topics, `api-init` runs `init_db()` (no
migration step) and generates the signing keypair, then the api serves the SPA
immediately and the collector/enricher start against the keys and tables
`api-init` left behind.

Verify:

```bash
docker ps                                    # hexgate-prod-* and hexgate-staging-* healthy —
                                             # except api-init/redpanda-init (one-shots: Exited (0))
                                             # and enricher (no healthcheck yet; check its logs)
curl -sf https://app.hexgate.ai/v1/health    # → 200
curl -sf https://app.staging.hexgate.ai/v1/health
# The /v1/traces route: a 401 means the collector answered (it rejects the
# unauthenticated POST); a JSON 404 means the api's SPA catch-all did, i.e. the
# path route is missing or ordered after the hostname's catch-all.
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://app.hexgate.ai/v1/traces   # → 401
# The MCP routes: /mcp answers 401 (no token) from the MCP server, and its
# metadata is JSON naming this hostname. A 502 means the mcp container is not
# up (docker compose ps / logs); a 404 or 405 means the api answered, i.e. the
# location is missing or mistyped. Run the same three against
# app.staging.hexgate.ai: metadata naming the prod hostname there means the
# staging server block points at prod's ports.
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://app.hexgate.ai/mcp        # → 401
curl -s https://app.hexgate.ai/.well-known/oauth-protected-resource/mcp           # → {"resource":"https://app.hexgate.ai/mcp",…}
curl -s https://app.hexgate.ai/.well-known/oauth-authorization-server             # → {"issuer":"https://app.hexgate.ai",…}
```

Then prove the pipeline behind the front door. From a laptop with the repo
checked out (needs an admin account on the stage — §5 — and an API key minted
in its dashboard):

```bash
export HEXGATE_API_KEY=fty_live_...          # minted on THIS stage; a prod key 401s on staging
export HEXGATE_SMOKE_EMAIL=you@example.com   # dashboard login on this stage
export HEXGATE_SMOKE_PASSWORD=...
make platform-smoke STAGE=prod               # or STAGE=staging
```

It sends eleven events (allow / deny / needs_approval decisions, one LLM
usage, one ban enforcement, and six LLM message events) through the SDK's OTLP
sender and polls the dashboard API until each shows up, then prints `PASS` or a
per-event `MISSING` list. Rows are tagged `agent_name = otlp_smoke` and a
per-run `session_id`, so they are easy to spot and harmless to leave. Run it
after every `platform-up`.

Five of the message events carry an input message past the SDK's 256 KiB cap,
and the check asserts each came back marked `truncated`, at full size, rather
than missing. Together they are ~1.25 MiB, which is deliberate: that is over
both the 1,000,000-byte record the collector's exporter defaults to and the
1 MiB the broker defaults to, so this run only passes on a stage that actually
got the record-size limits of `docs/internals/audit-pipeline.md` §4.1 —
altered topics, restarted collector and enricher — rather than merely a deploy
of the code. A stage that did not fails with **everything** `MISSING`, because
an oversized record is rejected whole and takes the decision spans with it.

**Read the traceback before blaming the stage.** All-`MISSING` has a second,
more common cause: the ~1.3 MB export goes out as one uncompressed POST inside
the exporter's 5 s deadline, so an uplink much below ~2 Mbit/s — a tether, hotel
wifi — never finishes the body and the whole batch is dropped locally. The two
are easy to tell apart in the ERROR lines above `flush complete`:
`Exception while exporting Span … TimeoutError` is this machine, `Failed to
export` is the collector refusing the batch. Re-run from a better connection
before altering any topics.

Exit 0 is the only green: 1 means an event did not land, and 2 means the
credentials above were missing so nothing was read back at all — the send step
on its own cannot fail, because the OTLP exporter only logs export errors.
Anything gating a deploy on this must treat 2 as a failure.

Scope: it starts at the SDK's sender, so a PASS proves the deployment —
proxy route, collector auth, Redpanda, enricher, ClickHouse, read API — for
this SDK version. It runs no agent, so the enforcer and adapter hooks that
produce these events in real use are not exercised; those are covered by
`pytest -m integration` against a local stack.

## 5. First admin (per env)

`HEXGATE_SEED=skip` → no seeded admin. Self-register on each env:

```bash
curl -X POST https://app.hexgate.ai/v1/auth/register \
  -H 'content-type: application/json' -d '{"email":"you@example.com","password":"<strong>"}'
```

(or Google sign-in if `HEXGATE_GOOGLE_CLIENT_ID/SECRET` are set — register the
`/v1/auth/google/callback` URL for each hostname).

## 6. Upgrades

```bash
cd /srv/hexgate-<stage> && git pull   # or checkout a new tag for prod

# Postgres DDL needs locks the running API holds. Stops api + enricher and
# leaves collector + redpanda up, so OTLP keeps buffering through the window.
make platform-stop-writers STAGE=<stage>

make platform-migrate STAGE=<stage>   # schema BEFORE images — see below; no-op when nothing is new
make platform-up STAGE=<stage>        # rebuilds changed images, recreates containers
```

`platform-migrate` runs first on purpose and is safe to run on every upgrade:
the files are idempotent, so a release that adds no column replays them to no
effect. Reversing the two is an outage, not a slower path (see below). Run it
unconditionally — including a first-ever deploy, where every Postgres file
guards each table it touches and skips with a `NOTICE` rather than failing on
one `create_all` has not built yet.

**Read the last line of the run, not the last file it named.** The two stores
are applied independently — a Postgres failure no longer skips ClickHouse — and
the run ends with `migrate(<stage>): postgres=… clickhouse=…` plus a nonzero
exit if either did not complete. A run that ends without that summary was
interrupted, and you cannot assume either store finished.

Postgres statements run under `lock_timeout=10s`. If you skip the `stop` above,
expect `canceling statement due to lock timeout` rather than a hang: an
`ADD COLUMN` waits behind any session idle in a transaction, and then blocks
every query queued behind it. Stop the writers and re-run — the files are
idempotent, so a partial run costs nothing.
`HEXGATE_MIGRATE_LOCK_TIMEOUT_MS=0` waits forever, for a quiesced stack running
a long index build.

Promote a release: tag it, `git checkout` it in the prod checkout, then the
same `platform-stop-writers` → `platform-migrate` → `platform-up` sequence,
plus any per-release step the migration table below names (e.g.
`platform-backfill` for `clickhouse/0005` and `clickhouse/0006`). Rolling
*back* past a release
that added columns has its own step — see below.

Upgrades reuse the env already on the box: the deploy targets only pull a
MISSING `.env.<stage>`, never refresh an existing one. If the secret changed,
refresh it first: `make platform-env-pull STAGE=<stage>`.

**When a release adds a required key** (the compose aborts with `required
variable X is missing a value` — on every target that shells out to compose,
since it interpolates the whole file for every subcommand, `platform-up`,
`platform-logs`, `platform-down` and `platform-stop-writers` alike; the running
stack is unaffected). That makes it the very FIRST step of the recipe above,
so do the refresh before starting the upgrade. The order is: an admin adds
the key to the `/hexgate/<stage>` secret (new version), then on the box
`make platform-env-pull STAGE=<stage>`, then `make platform-up`. Never
hand-edit `.env.<stage>` — the next pull would drop the change. Releases so
far that did this:

| Release | Key | Value |
|---|---|---|
| OTLP pipeline (collector/redpanda/enricher) | `HEXGATE_OTLP_PORT` | `7001` prod, `7201` staging |
| MCP server | `HEXGATE_MCP_PORT` | `7002` prod, `7202` staging |
| MCP server | `HEXGATE_PUBLIC_URL` | `https://app.hexgate.ai` prod, `https://app.staging.hexgate.ai` staging — no trailing slash (the api and mcp refuse to boot with one) |
| MCP server | `HEXGATE_MCP_RESOURCE_URL` | `https://app.hexgate.ai/mcp` prod, `https://app.staging.hexgate.ai/mcp` staging |

The MCP server release also changes the box's reverse proxy, which
`platform-up` does not touch. In the same upgrade, apply section 3's "The MCP
routes" and "Rate-limit" blocks as written there, once per stage with that
stage's ports, then `nginx -t && systemctl reload nginx` and run section 4's
MCP checks against both hostnames. Until then the api answers `/mcp` in the MCP
server's place and the OAuth paths run with no per-IP limit.

**When a release adds a Postgres column** — `init_db()` is `create_all`, which
creates missing tables but never adds columns to an existing one, so the schema
change has to be applied *before* the new images go up. Files live in
`platform/postgres/migrations/`, are idempotent, and are applied by
`make platform-migrate STAGE=<stage>` (the step in the recipe above; it replays
the whole directory against the stack's Postgres).

**When a release adds a ClickHouse table or column** — `init/schema.sql` runs
only on an empty volume, so an existing stage gets the change from
`platform/clickhouse/migrations/`, replayed by the same `platform-migrate`
step (every file is `IF NOT EXISTS`, so replay is a no-op once applied). The
ordering matters more here than for Postgres: the api and enricher check the
tables they write and read at boot (`verify_all`) and refuse to start on a
missing one, and `platform-up` has already recreated their containers by then
— the old ones are gone, so a skipped migration is a crash loop of the whole
API, not a degraded message path.

Applying any of them *after* the deploy is not a slower path, it is an outage —
and the signature differs per migration, so don't debug one expecting the other.
Both directories number from `0001`, so the prefixes below are load-bearing.

`postgres/0001` takes OTLP ingest down: the collector's snapshot query selects
`revoked_at`, so it refuses to boot, and every bearer-authenticated API route
500s alongside it.

`postgres/0002` leaves the collector running — its query names its columns and
none of them are new — but it is otherwise *wider* than `postgres/0001`, not
narrower. `devtoken` is one of its nine tables, so `find_token_by_secret` 500s
and every bearer-authenticated route goes down exactly as under `postgres/0001`;
SDK traffic through the API stops, and only OTLP ingest is spared. The other
eight tables take the cookie-authenticated routes with them, reads included, so
the dashboard goes blank rather than degrading.

`postgres/0003` is the same failure, scoped to one table: routes touching
`policy_file` (the compose entry-file store) 500 while the rest of the control
plane is fine.

`postgres/0005` is scoped the same way: every route touching
`oauth_refresh_token` or `oauth_authorization_code` 500s, so MCP clients can't finish a login or refresh,
while the rest of the control plane is fine.

The ClickHouse migrations fail differently again — not a 500 on the routes that
touch the table, but the boot-time `verify_all` crash loop described above.

Every migration here is safe to run early — the *old* code never selects the new
columns — which is why the step is unconditional in the recipe. The exceptions
are `clickhouse/0005` and `clickhouse/0006`: neither may run while a writer is
running — see *Materialized views and the writers* below.

| Release | Migration |
|---|---|
| devtoken soft delete | `postgres/0001_devtoken_soft_delete.sql` |
| control-plane actor columns | `postgres/0002_actor_columns.sql` |
| actor trail on the compose file store | `postgres/0003_policy_file_actor_columns.sql` |
| OAuth refresh tokens linked to their authorization code, a replay mark and an `expires_at` index on the code | `postgres/0005_oauth_refresh_code_link.sql` |
| policy_decision `attributes` | `clickhouse/0001_add_policy_decision_attributes.sql` |
| run attribution on decisions/usage | `clickhouse/0002_add_run_columns.sql` |
| LLM message logging (`llm_message` table) | `clickhouse/0003_add_llm_message.sql` — required before the build that stores `hexgate.messages` |
| run counting (`agent_run` table) | `clickhouse/0004_add_agent_run.sql` — required before the enricher build that stores `hexgate.runs` |
| agent usage rollup (`usage_minute` + 3 views) | `clickhouse/0005_add_usage_minute.sql` — apply with the writers stopped, then `make platform-backfill STAGE=<stage> FILE=0005_usage_minute` before `platform-up` |
| user usage rollup (`usage_minute_user` + 3 views) | `clickhouse/0006_add_usage_minute_user.sql` — apply with the writers stopped, then `make platform-backfill STAGE=<stage> FILE=0006_usage_minute_user` before `platform-up`; required before the API build that serves `/v1/usage/users:read` |

**Materialized views and the writers (`0005`, `0006`).** The `usage_minute` views sum
every row inserted into their source tables, duplicates included, and only an
enricher from the same release dedups retried spans across polls. So for this
release `platform-stop-writers` is mandatory even though nothing in Postgres
changes: never let `0005` run beside an older enricher, or every retried span
is counted twice, permanently. Then backfill once, still with the writers
stopped, so no row is both backfilled and counted by a view:

```bash
make platform-stop-writers STAGE=<stage>
make platform-migrate STAGE=<stage>
make platform-backfill STAGE=<stage> FILE=0005_usage_minute   # no-op after a successful run
make platform-up STAGE=<stage>
```

A forgotten backfill only means `30d` usage reads start near zero. To rebuild
the rollup (a late backfill, a backfill that failed partway, a re-upgrade after
a rollback that kept the table, or a count that looks wrong): stop the writers,
`TRUNCATE TABLE hexgate_audit.usage_minute`, run the backfill, start the writers.

`0006`'s hazard is a writer running beside the migration and backfill, not
old-enricher duplicates. A row written after the `CREATE` of the
`usage_minute_user` views but before the backfill leaves the table non-empty,
so the backfill's empty-table guard skips it entirely and `30d` user reads
start near zero, silently. A row written while the backfill runs is counted by
a view and again by the backfill, for good. So the same recipe holds, with
`FILE=0006_usage_minute_user`. On a release that ships both, run
the two backfills back to back in the same stopped window. To rebuild the user
rollup: stop the writers, `TRUNCATE TABLE hexgate_audit.usage_minute_user`, run
the backfill, start the writers.

**SDK release order for a new span scope.** The enricher sends any scope it
does not know to the DLQ as `unknown_scope`. So deploy the enricher build that
stores `hexgate.runs` to every stage *before* releasing an SDK that emits it:
otherwise every run start becomes a DLQ record on a stage still running the old
enricher. No other data is lost, and deploying the enricher stops it. To roll
back, go the other way: revert the SDK release first, then the enricher.

**Rolling back past a migration.** The columns stay behind when the code goes
away, and old code does not know to filter on them. Nothing here is automatic,
so a rollback that skips the compensating step below is silent — the stack comes
up healthy and behaves wrongly.

*Past `0001_devtoken_soft_delete`* — revocation became a soft delete: the row
survives with `revoked_at` stamped, and every read filters it out. The
pre-soft-delete code has no such filter, so it resolves those rows as live keys.
Before checking out a tag older than that release:

```bash
cd /srv/hexgate-<stage>
docker compose -p hexgate-<stage> --env-file platform/.env.<stage> \
  -f platform/docker-compose.deploy.yml exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U hexgate -d hexgate \
  -c "DELETE FROM devtoken WHERE revoked_at IS NOT NULL"
```

Skipping it does not expose every surface equally. Revoke also masks the
secret, and the pre-soft-delete code resolves a key by matching that secret
exactly, so it can never match a revoked row: bearer HTTP and the WebSocket
handshake stay refused either way. Two surfaces do regress, both of them ones
that never look at the secret — **OTLP ingest**, which matches the token's
`token_id` fact against a snapshot of the whole key table and finds the row
present again, and the **dashboard's key list**, which reads the rows back
unfiltered. Run it anyway: those are the surfaces the compensating step exists
for. It discards the audit rows, which is the point — the old schema has
nowhere to keep them.

*Past `0002_actor_columns`* and *past `0003_policy_file_actor_columns`* —
**no compensating step.** Every column they add is nullable and unread by the
older code, so leave them in place. What the rollback cannot undo is the keys
that member removal revoked while the new code was live — correctly so, and
irreversibly: `postgres/0001` masked the secret on revoke, so clearing `revoked_at`
would not bring the credential back. Mint a fresh key instead.

*Past `clickhouse/0005_add_usage_minute`* — drop the three views **before** an
older enricher starts, or its cross-poll duplicates are summed into
`usage_minute` again. Empty the table too: nothing older reads it, and a
re-upgrade backfills only an empty `usage_minute`, so rows left behind would
hide the whole rollback window from every `30d` count.

```bash
cd /srv/hexgate-<stage>
docker compose -p hexgate-<stage> --env-file platform/.env.<stage> \
  -f platform/docker-compose.deploy.yml exec -T clickhouse sh -c \
  'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery' <<'SQL'
DROP VIEW IF EXISTS hexgate_audit.usage_minute_from_runs;
DROP VIEW IF EXISTS hexgate_audit.usage_minute_from_decisions;
DROP VIEW IF EXISTS hexgate_audit.usage_minute_from_llm;
TRUNCATE TABLE IF EXISTS hexgate_audit.usage_minute;
SQL
```

*Past `clickhouse/0006_add_usage_minute_user`* — **no step is needed** while the
enricher stays at or after `0005`: older API builds never read the table, and
its views keep counting correctly, so a re-upgrade finds it current and the
backfill guard skips it. Rolling back past `0005` as well: drop the three
`usage_minute_user_from_*` views alongside the agent ones, and empty the table,
for the same reason as `0005`:

```bash
cd /srv/hexgate-<stage>
docker compose -p hexgate-<stage> --env-file platform/.env.<stage> \
  -f platform/docker-compose.deploy.yml exec -T clickhouse sh -c \
  'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery' <<'SQL'
DROP VIEW IF EXISTS hexgate_audit.usage_minute_user_from_runs;
DROP VIEW IF EXISTS hexgate_audit.usage_minute_user_from_decisions;
DROP VIEW IF EXISTS hexgate_audit.usage_minute_user_from_llm;
TRUNCATE TABLE IF EXISTS hexgate_audit.usage_minute_user;
SQL
```

**When a release changes the topic config, re-run `redpanda-init` by hand** —
`platform-up` will not. `create-topics.sh` reconciles `retention.ms` and
`max.message.bytes` on every run, but `redpanda-init` is the only thing that
invokes it, and `restart: "no"` + `service_completed_successfully` means an
existing stack never starts it again. The collector and enricher take their new
limits from rebuilt images, so skipping this leaves the broker as the one hop
still on the old, narrower limit — and it rejects the records everything else
is now sized to send:

```bash
cd /srv/hexgate-<stage>
docker compose -p hexgate-<stage> --env-file platform/.env.<stage> \
  -f platform/docker-compose.deploy.yml run --rm redpanda-init
# verify, then restart the two clients of that limit
docker compose -p hexgate-<stage> --env-file platform/.env.<stage> \
  -f platform/docker-compose.deploy.yml exec redpanda \
  rpk topic describe hexgate.otlp.raw --brokers localhost:9092
docker compose -p hexgate-<stage> --env-file platform/.env.<stage> \
  -f platform/docker-compose.deploy.yml restart collector enricher
```

`run --rm` starts a sibling container rather than reusing the completed one, so
it works on a running stack and leaves nothing behind. Releases so far that
needed this:

| Release | Change |
|---|---|
| LLM message logging | `max.message.bytes` → 8 MiB on `hexgate.otlp.raw` **and** `hexgate.otlp.dlq` |

## Operations

**Back up these volumes** (per env, prefixed `hexgate-prod_` / `hexgate-staging_`):

| Volume | Holds | If lost |
|---|---|---|
| `pg-data` | control-plane DB | total data loss |
| `ch-data` | audit log | audit history lost |
| `hexgate-keys` | signing/session keystore | every token/session/bundle invalidated |
| `redpanda-data` | span buffer + DLQ (`hexgate.otlp.raw` 3d, `hexgate.otlp.dlq` 30d) | not backed up, by design: raw spans are re-derivable from nothing and ClickHouse is the system of record; the DLQ holds up to 30 days of rejected-span evidence, which a rebuild loses (topics themselves are recreated by `redpanda-init` on the next `up`) |

The box's reverse proxy holds the TLS certs — back those up per its own docs
(losing them risks Let's Encrypt rate limits on re-issue).

**Single API instance per env** — the in-process WS relay can't span workers or
hosts (`platform/api/Dockerfile`). Scale up, not out.

**Keystore restores**: `hexgate.pub` is written only when the keypair is first
generated. A `hexgate-keys` volume restored from a backup of `hexgate.priv`
alone boots the api fine but leaves the collector crash-looping on the missing
public key — restore both files (or regenerate the public half by hand from
the private one) until the keystore learns to rewrite it on load.

**Revocation cache tuning** — `poll_interval` (20s) is how long a revoked key
still works; `max_staleness` (1h) is how long the collector serves its last
snapshot while refreshes *fail*, after which it rejects every request. Tune per
stage with `HEXGATE_COLLECTOR_REVOCATION_POLL_INTERVAL` /
`HEXGATE_COLLECTOR_REVOCATION_MAX_STALENESS` in `.env.<stage>`; never set either
blank (the collector refuses to boot). Lowering `max_staleness` below a few
minutes makes routine database maintenance total span loss, reported as a green
healthcheck.

**Schema changes** apply only on an empty volume; changing one after first boot
goes through `platform/clickhouse/migrations/`, applied by `make platform-migrate`
(§6).

## Consumer SDK

Point the SDK at the env's public origin (no `/v1`, no port — the proxy handles
both):

```bash
export HEXGATE_API_URL=https://app.hexgate.ai     # or app.staging.hexgate.ai
export HEXGATE_API_KEY=fty_live_...                    # token minted in that env's dashboard
```
