"""Start-up configuration. Nothing is read from the environment yet; when
settings are added they come from the process environment only (prefix
``HEXGATE_ASSISTANT_``), never from the API's ``.env``."""

from dataclasses import dataclass

# The port the server listens on, fixed here and not configurable.
PORT = 8090


@dataclass(frozen=True)
class Settings:
    # All interfaces, so a container's published port reaches it.
    bind_host: str = "0.0.0.0"
    port: int = PORT

    @classmethod
    def from_env(cls) -> "Settings":
        return cls()
