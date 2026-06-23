"""Provision one disposable demo world's serve token.

Run once per container before (or alongside) the API process. Shares the
container's SQLite file and on-disk keystore with the API, so the minted
``HEXGATE_KEY`` verifies against the same signing key the API serves with.

Everything here is idempotent — ``init_db`` + ``ensure_default_seed`` +
``ensure_keypair`` are no-ops on a warm DB, so it's safe to run before
uvicorn (which re-runs them in its lifespan).
"""

from __future__ import annotations

import asyncio
import sys

# The API package is imported by module name (uvicorn runs `main:app` with the
# api dir on sys.path), so callers must add platform/api to sys.path first.


async def _mint() -> str:
    from db import async_session_factory, init_db
    from main import keystore  # the same FileKeyStore instance the API uses
    from services import DEFAULT_PROJECT_ID, ensure_default_seed, mint_dev_token

    await init_db()
    keystore.ensure_keypair()
    async with async_session_factory() as session:
        await ensure_default_seed(session)
        _, full_token = await mint_dev_token(
            session,
            DEFAULT_PROJECT_ID,
            name="demo-serve",
            # Same scopes the dashboard's mint UI issues by default — these are
            # what the per-user attenuation flow (`user_attenuation`) needs.
            scopes=["mint_user_token", "read_audit"],
            env="live",
            signing_key_bytes=keystore._private_key_bytes(),
        )
    return full_token


def provision_serve_token() -> str:
    """Return a fresh ``fty_live_...`` HEXGATE_KEY scoped to the seeded project."""
    return asyncio.run(_mint())


if __name__ == "__main__":
    # Print the token so a shell caller can capture it: HEXGATE_KEY=$(python provision.py)
    sys.stdout.write(provision_serve_token())
