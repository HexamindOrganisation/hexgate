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

The box **already runs nginx** (host-installed, one `sites-available` file per
hostname) that owns 80/443 and terminates TLS. Hexgate does not ship its own
front proxy — you point the existing one at the three loopback ports by
hostname and path.

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

Images build **on the box** — the box pulls nothing from a registry. amd64
throughout (the API bundles the amd64-only OPA binary; the image also runs a
Node build stage to bundle the dashboard SPA). Releases also publish the same
images to GHCR for self-hosters: see [Published images](#published-images).

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
collector's OTLP/HTTP receiver — and must match BEFORE the hostname's catch-all
route (the exact `location = /v1/traces` that the stage's include provides, see
*The nginx includes* below, never written by hand). This is what makes the
SDK's default OTLP endpoint (`<api url>/v1/traces`) work with zero client
config. Without the route, the api's GET-only SPA catch-all answers the POST
with a 405 and every SDK on that stage silently loses its audit trail — `make
platform-smoke` (§4) is how you catch that. Everything else in the stack
(Postgres, ClickHouse, Redpanda) binds no host ports; Redpanda in particular is
PLAINTEXT with no auth — never publish it.

**The `/v1/traces` route carries its own request-body limit.** The collector's
OTLP receiver accepts 32 MiB (`max_request_body_size`) and an SDK export of LLM
message spans can reach ~17 MiB, but the proxy enforces its own cap *first* and
answers `413` before the collector ever sees the POST — so the proxy, not the
collector, is what decides whether a large export gets through. It is the one
hop in the record-size budget (`docs/internals/audit-pipeline.md` §4.1) that the
box applies, not the containers: `make nginx-check` checks its syntax and
fails if it no longer equals the collector's `max_request_body_size`, so the
two change together.

In nginx the setting is `client_max_body_size`, and it belongs **inside the
`location = /v1/traces` block** rather than at `http` or `server` level. That
block lives in the repo, in
[`platform/nginx/hexgate-prod.conf`](nginx/hexgate-prod.conf) and
[`hexgate-staging.conf`](nginx/hexgate-staging.conf) (see *The nginx includes*
below).

Note the value here is deliberately *tighter* than the surrounding default —
on the prod box the `http` block allows `100M`, and this route is held to a
much smaller number on purpose. `/v1/traces` is the only endpoint taking bulk
binary uploads, and nginx enforces the limit **before** the collector verifies
the Biscuit token, so whatever it allows is what an unauthenticated caller can
make the proxy buffer. Keep it as small as the largest legitimate export: 32
MiB covers a 64-span batch of maximally-sized message spans, and nothing more.
The value was 8 MiB before message logging, which is why it had to be raised.

**The nginx includes.** Every Hexgate route on the box except the hostname's
catch-all is in `platform/nginx/hexgate-<stage>.conf`, and each stage's server
block includes its own file from its own checkout. A route change then ships
when that checkout moves to it and nginx reloads (section 6), and `make
nginx-check` (also a CI job) runs `nginx -t` on each stage's files inside a
stub shaped like its server block, and once on both stages together as the box
includes them, and fails if staging's files differ from prod's beyond their
ports and zone names, if a location proxies to a port its route table does not
name, or if the `/v1/traces` body cap no longer equals the collector's. The
server block keeps its listen, TLS, forwarded headers and `location /` to the
api.

Install once per stage. In `sites-available/app.hexgate.ai`, inside the 443
`server` block, replace the hand-written `location = /v1/traces { … }` (on a
fresh box, where there is none, add it beside `location /`) with

```nginx
include /srv/hexgate-prod/platform/nginx/hexgate-prod*.conf;
```

and in `sites-available/app.staging.hexgate.ai` with

```nginx
include /srv/hexgate-staging/platform/nginx/hexgate-staging*.conf;
```

and, once per box (not per stage: it holds both stages' lines, and a line
included twice declares its zones twice, which fails `nginx -t`), create
`/etc/nginx/conf.d/hexgate.conf` at `http` level with each stage's rate-limit
zones (see *The MCP routes and rate limits* below):

```nginx
include /srv/hexgate-prod/platform/nginx/hexgate-zones-prod*.conf;
include /srv/hexgate-staging/platform/nginx/hexgate-zones-staging*.conf;
```

Remove the old block in the same edit: the include defines `location =
/v1/traces` too, and two of them fail `nginx -t`. Also remove any MCP or OAuth
`location` blocks, `limit_req_status` line or `conf.d/hexgate-ratelimit.conf`
hand-written from an earlier version of this page: the include and
`hexgate.conf` now provide them (the duplicate locations and `limit_req_status`
fail `nginx -t`). `hexgate-ratelimit.conf` is shared by both stages, so delete
it only once neither server block has a `limit_req` naming `hexgate_mcp` or
`hexgate_oauth`; until then it does no harm. Then `sudo nginx -t && sudo
systemctl reload nginx`; a graceful reload keeps existing connections, and a
failed test changes nothing. The include names a file in that stage's checkout,
so install it only once the checkout has `platform/nginx/`: for prod, after
promoting a release that contains it. The wildcard also hides a mistyped path
or an include installed too early (both match nothing and pass `nginx -t`), so
finish with the section 4 check `curl -s -o /dev/null -w '%{http_code}\n' -X
POST https://<hostname>/v1/traces`: 401 is the collector; a 405 means the
include matched nothing.

The `*` matters: nginx treats a wildcard include that matches nothing as empty,
while a plain path to a missing file is an error. One nginx serves both stages
and the box's other sites, and with a plain path a checkout without the file (a
rollback, an old branch) would fail every later reload on the box, certbot's
renewal reload included, and the next restart would not come back. With the
wildcard that checkout only loses its Hexgate routes: `/v1/traces` then goes to
the api, which answers 405, and the stage's SDKs drop their spans, which `make
platform-smoke` catches. A file that exists but is broken still fails `nginx
-t`, so test right after a checkout moves and reload once the stack is up (both
in the section 6 recipe): a bad file shows up before anything changes, while
the running config is still the old one.

Each hostname is one nginx `server` block proxying to its loopback ports.
Forward `X-Forwarded-Proto: https` — the API trusts it, via uvicorn
`--proxy-headers`, for correct https OAuth callbacks — and set
`proxy_set_header Host $host`. The images trust those headers only from
the peers in `FORWARDED_ALLOW_IPS` (loopback by default); the deploy compose
sets it to `*` on the api and mcp services, which is safe only because their
ports are published on `127.0.0.1`. `/v1` includes WebSocket endpoints, so the
server block also needs the `Upgrade`/`Connection` headers set.

**The MCP routes and rate limits** are in the same files. `/mcp` (a `^~`
prefix: the MCP server redirects `/mcp/` to `/mcp`) and its protected-resource
metadata, `/.well-known/oauth-protected-resource/mcp`, go to the stage's
`HEXGATE_MCP_PORT`; the api's two discovery documents
(`/.well-known/oauth-authorization-server`, `/.well-known/jwks.json`, exact
matches so an ACME or `security.txt` location is never shadowed) and
`/v1/oauth/` go to the api. All five are limited per client IP, because they
answer callers with no credentials by design and `/v1/oauth/authorize` and
`/token` make the api fetch a client metadata document from whatever host the
caller names. The comments in `hexgate-<stage>.conf` give each block's reason.
The MCP server fetches the JWKS from `http://api:8000` on the compose network,
never through the proxy. `HEXGATE_PUBLIC_URL` (section 6) must name this same
hostname: the compose derives the MCP resource URL from it as
`HEXGATE_PUBLIC_URL` + `/mcp`, and a client that connects to
`https://app.hexgate.ai/mcp` is only given a token for the resource URL the MCP
server advertises.

The limits' zones are per stage, in
`platform/nginx/hexgate-zones-<stage>.conf`, at `http` level, included from
`conf.d/hexgate.conf` (the install above).

Each checkout then brings its zones and the `limit_req` lines that name them
together, or neither. One shared zones file could only come from one checkout,
and a stage ahead of it would name a zone nginx does not know, which fails
every reload on the box. The file name is deliberately outside the server
include's `hexgate-<stage>*.conf`, since a zone directive at `server` level is
an error. Put every `proxy_set_header` at `server` level and none inside a
`location`, this server's `location /` included: a `location` that sets any
header of its own inherits none of the `server`'s. That covers
`X-Forwarded-Proto`, `Host $host` and the WebSocket `Upgrade`/`Connection` pair
(above). For `/mcp`, `Host $host` matters: the MCP server's `/mcp/` → `/mcp`
redirect is built from the Host it receives (uvicorn's proxy headers correct
the scheme, not the host), so without it the redirect points at
`127.0.0.1:7002`.

`$binary_remote_addr`, the limits' key, is the client's address only because
nothing sits in front of this proxy; put a CDN or load balancer in front of it
and the key becomes that hop's address, throttling every user as one.

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
# unauthenticated POST); a 405 means the api's GET-only SPA catch-all did,
# i.e. the stage's include is missing or matched nothing.
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
sudo nginx -t                          # a bad platform/nginx/ file stops here, before anything changes

# Postgres DDL needs locks the running API holds. Stops api + enricher and
# leaves collector + redpanda up, so OTLP keeps buffering through the window.
make platform-stop-writers STAGE=<stage>

make platform-migrate STAGE=<stage>   # schema BEFORE images — see below; no-op when nothing is new
make platform-up STAGE=<stage>        # rebuilds changed images, recreates containers

# Every time: the box reads platform/nginx/ only on a reload, and a checkout
# move doesn't say whether it changed. After platform-up, so a new route's port
# is already listening; a graceful reload keeps connections. Root: the
# checkouts are not, nginx is.
sudo nginx -t && sudo systemctl reload nginx
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
`platform-backfill` for `clickhouse/0005` and `clickhouse/0006`), and the nginx
reload at the end of the recipe. The first release that contains
`platform/nginx/` also needs the one-time section 3 install on that stage (the
server include and the `conf.d/hexgate.conf` zones lines); until then its
server block keeps the hand-written block, and routes added to
`platform/nginx/` later never reach it. Rolling *back* past a release that
added columns has its own step — see below.

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

The MCP server release adds its routes to `platform/nginx/` and a zones file
per stage, which nginx needs at `http` level first. In the same upgrade, before
the recipe's `git pull`, create `conf.d/hexgate.conf` from section 3 if the box
does not have it (each line matches nothing until its checkout has the file),
so no reload in between sees a `limit_req` naming a zone that is not declared;
`nginx -t` reports one as `zero size shared memory zone "hexgate_mcp_<stage>"`.
If that stage does not have the server include yet, install it after the pull,
as section 3 says. If it has hand-written MCP or OAuth blocks from an earlier
version of this page, remove them before the pull (section 3 lists them): the
include now defines them, and duplicates fail `nginx -t`. After the reload, run
section 4's MCP checks against both hostnames.

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

**Rolling back past `platform/nginx/`.** A commit from before the per-stage
includes has no `platform/nginx/hexgate-<stage>.conf`, so the wildcard include
matches nothing and that stage loses `/v1/traces` (section 3). Before checking
such a commit out, put the hand-written block back in that stage's server block
in place of the `include` line, with that stage's collector port (7001 prod,
7201 staging), and reload. Moving forward past it again, redo the section 3
install (the `include` in place of this block), or routes added to
`platform/nginx/` later never reach the box:

```nginx
location = /v1/traces {
    proxy_pass http://127.0.0.1:7001;   # 7201 on staging
    client_max_body_size 32m;
    proxy_connect_timeout 10s;
    proxy_read_timeout 30s;
}
```

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

## Published images

Each real release (`release.yml`, target `pypi`) also publishes `hexgate-api`,
`hexgate-mcp` and `hexgate-collector` to
`ghcr.io/hexamindorganisation/`, through `.github/workflows/docker-publish.yml`
behind the `docker-release` environment's reviewers. To (re)publish an existing
tag by hand, dispatch that workflow from `main` with the tag.

Each image is built from the tag, scanned (the publish fails on a fixable
HIGH/CRITICAL finding unless `.trivyignore` lists it), pushed with SBOM and
provenance attestations, and signed with cosign. Tags are the version
(`0.3.4`) and the commit (`sha-<commit>`); there is no `latest`. Pull by
digest, after checking the signature:

```bash
cosign verify ghcr.io/hexamindorganisation/hexgate-api@sha256:<digest> \
  --certificate-identity https://github.com/HexamindOrganisation/hexgate/.github/workflows/docker-publish.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The deploy compose can run them instead of building:
`REGISTRY=ghcr.io/hexamindorganisation TAG=<version>`, then
`docker compose ... pull` and `up --no-build`.

Running them anywhere else:

- **`FORWARDED_ALLOW_IPS`** (api, mcp): the peers whose `X-Forwarded-*`
  headers are trusted, loopback by default. Set it to your proxy's address,
  never `*` on a port the internet can reach.
- **Volumes** (api): `/keys` holds the signing keys and must persist; `/data`
  holds the SQLite database, used only when no Postgres is configured.
- **`HEXGATE_DEMO`** (api): leave it unset. It enables a passwordless admin
  login for throwaway demo containers.
- **Collector:** set `HEXGATE_COLLECTOR_POSTGRES_DSN`. The default in its
  config is the local dev password, which it refuses on a non-local host.

## Consumer SDK

Point the SDK at the env's public origin (no `/v1`, no port — the proxy handles
both):

```bash
export HEXGATE_API_URL=https://app.hexgate.ai     # or app.staging.hexgate.ai
export HEXGATE_API_KEY=fty_live_...                    # token minted in that env's dashboard
```
