"""Start-up configuration, read from the process environment only (prefix
``HEXGATE_ASSISTANT_``), never from the API's ``.env``."""

import os
from dataclasses import dataclass

# The port the server listens on, fixed here and not configurable.
PORT = 8090


@dataclass(frozen=True)
class Settings:
    # Loopback unless the environment says otherwise, so `make assistant-run`
    # is not reachable from the network. The image opts in to all interfaces.
    bind_host: str = "127.0.0.1"
    port: int = PORT

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bind_host=os.environ.get("HEXGATE_ASSISTANT_BIND_HOST", cls.bind_host)
        )
