# Deploying Hexgate on Scaleway

This is the runbook for the production stack defined in
[`docker-compose.prod.yml`](docker-compose.prod.yml): four containers on one
private network — **postgres**, **clickhouse**, **api**, and **edge** (Caddy).
Only the edge is internet-facing; it terminates TLS, serves the dashboard SPA,
and reverse-proxies `/v1/*` to the API.

```
                  ┌──────────────────────────────┐
 Internet ──TLS──▶ edge (Caddy) :80/:443          │
                  │  • serves dashboard SPA        │
                  │  • reverse_proxy /v1/* → api   │ (HTTP + WS)
                  │  • auto Let's Encrypt cert     │
                  └───────────────┬────────────────┘
                                  │ private compose network
            ┌─────────────────────┼─────────────────────┐
            ▼                     ▼                       ▼
          api:8000           postgres:5432         clickhouse:8123
```

Images are **built on the machine** from this repo — no container registry
required. (Pulling prebuilt images from a registry is possible too; see the
appendix.)

## Prerequisites

- A Scaleway **amd64** instance with Docker + the Compose plugin. The API image
  bundles the amd64-only OPA binary, so build and run on amd64.
- A **domain name** you control.

Sizing note: the build runs on the box — the edge image does a full `pnpm
install` + Vite build and the API image downloads OPA + syncs Python deps. Give
the instance enough RAM/CPU (≈2 GB+); a tiny instance may be slow or OOM during
the first build.

## 1. DNS first

Point an **A-record** for your domain at the instance's public IP **before**
starting the stack. Caddy obtains its Let's Encrypt cert via an HTTP-01
challenge on port 80 — if DNS doesn't resolve to the box yet, issuance fails.
Make sure ports **80 and 443** are open in the instance's security group.

## 2. Get the repo onto the instance

```bash
git clone <repo-url> hexgate && cd hexgate
# or, on an existing checkout: git pull
```

## 3. Configure

```bash
cp platform/.env.prod.sample platform/.env.prod
# edit platform/.env.prod — see the inline comments. At minimum:
#   HEXGATE_DOMAIN                             (your domain)
#   HEXGATE_POSTGRES_PASSWORD + DATABASE_URL   (same password in both)
#   HEXGATE_CLICKHOUSE_PASSWORD
# Generate secrets with: openssl rand -base64 32
# REGISTRY/TAG are not needed for build-on-target — leave them unset.
```

`platform/.env.prod` is gitignored — it holds real secrets, never commit it.

## 4. Build + launch

```bash
make platform-prod-up      # builds the api + edge images locally, then up -d
```

First boot, in order:
- postgres + clickhouse come up; their healthchecks gate the API start.
- clickhouse runs `clickhouse/init/schema.sql` **once** on the empty volume.
- the API runs `init_db()` (SQLAlchemy `create_all`) automatically — no
  migration step.
- edge starts, requests its cert, and begins serving.

Check health:

```bash
make platform-prod-logs                                 # watch it come up (Ctrl-C to stop)
docker compose -f platform/docker-compose.prod.yml ps   # all healthy?
curl -sf https://<domain>/v1/health                     # → 200
curl -sfI https://<domain>/                             # → 200, SPA
```

`make platform-prod-down` stops everything (data volumes are kept).

## 5. First admin

The prod env sets `HEXGATE_SEED=skip`, so there is **no passwordless seeded
admin**. Bootstrap the first account by self-registration:

```bash
curl -X POST https://<domain>/v1/auth/register \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"<strong>"}'
```

…or sign in with Google if you configured `HEXGATE_GOOGLE_CLIENT_ID/SECRET`
(register `https://<domain>/v1/auth/google/callback` in the Google console).

## Upgrades

```bash
git pull
make platform-prod-up      # rebuilds changed images and recreates containers
```

## Operations

**Stateful volumes — back these up:**

| Volume | Holds | If lost |
|---|---|---|
| `pg-data` | control-plane DB (users, orgs, policies) | total data loss |
| `ch-data` | audit log | audit history lost |
| `hexgate-keys` | signing/session keystore | **every token, session, and signed bundle is invalidated** |
| `caddy-data` | TLS certs + ACME account | certs re-issued; risks Let's Encrypt rate limits |

**Scaling: single API instance only.** The API holds WebSocket relay state
in-process, so it cannot run multiple workers or be horizontally scaled (see
the `--workers 1` rationale in `platform/api/Dockerfile`). Scale up, not out.

**Schema changes.** clickhouse `schema.sql` and the API's `create_all` only run
against *empty* state. Changing a schema after first boot requires a manual
migration — they do not auto-apply on an existing volume.

## Appendix — deploying from a registry (optional)

For multiple machines, a CI pipeline, or instant tag-based rollback, you can
build once and pull prebuilt images instead of building on each box. Set
`REGISTRY` and `TAG` in `.env.prod`, build/push the two images
(`hexgate-api`, `hexgate-edge`, both amd64) to your registry, then on the box
run the stack **without** `--build` so it pulls:

```bash
docker compose -f platform/docker-compose.prod.yml --env-file platform/.env.prod up -d
```
