"""Run the server: ``python -m hexgate_assistant``."""

import logging

import uvicorn

from hexgate_assistant.server import GRACEFUL_SECONDS, create_app
from hexgate_assistant.settings import Settings

if __name__ == "__main__":
    # Log lines carry their level and logger, so a WARNING names its source.
    logging.basicConfig(level=logging.INFO)
    # Trust X-Forwarded-Proto from the box's reverse proxy (TLS terminates
    # upstream), as platform/api/Dockerfile does. Only from the addresses in
    # FORWARDED_ALLOW_IPS (uvicorn reads it; loopback when unset), never from
    # everyone: the published image may run without a proxy in front, where
    # any client could forge the headers.
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.port,
        proxy_headers=True,
        # Without it uvicorn waits for every open turn stream before it runs
        # the shutdown that stores and unlocks the turns.
        timeout_graceful_shutdown=GRACEFUL_SECONDS,
    )
