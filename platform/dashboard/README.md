# Hexgate Dashboard

React + TypeScript + Vite frontend for the Hexgate control plane. Proxies `/v1/*` to the platform API on `:8000`.

## Setup

```bash
# Run from the repo root
make dashboard-install
```

## Dev server

```bash
# Run from the repo root
make dashboard  # starts Vite on :5173
```

## Code quality

```bash
# Run from the repo root
make dashboard-fmt        # Prettier auto-fix
make dashboard-fmt-check  # Prettier check only

# Run from platform/dashboard/
pnpm lint  # ESLint
```

## Tests

```bash
# Run from platform/dashboard/
pnpm test        # single run
pnpm test:watch  # watch mode (reruns on file save)
```
