"""Run the server: ``python -m hexgate_mcp``."""

import logging

import uvicorn

from hexgate_mcp.server import create_app

if __name__ == "__main__":
    # Runs before FastMCP's own basicConfig (bare "%(message)s"), so log lines
    # carry their level and logger: a WARNING refusal is a config mismatch.
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=8080)
