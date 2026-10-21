"""Run the server: ``python -m hexgate_mcp``."""

import logging

import uvicorn

from hexgate_mcp.server import create_app

if __name__ == "__main__":
    # Runs before FastMCP's own basicConfig (bare "%(message)s"), so log lines
    # carry their level and logger: a WARNING refusal is a config mismatch.
    logging.basicConfig(level=logging.INFO)
    # Trust X-Forwarded-Proto from the box's reverse proxy (TLS terminates
    # upstream), as platform/api/Dockerfile does: otherwise the redirect from
    # /mcp/ to /mcp points at http:// and the client's retry fails.
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=8080,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
