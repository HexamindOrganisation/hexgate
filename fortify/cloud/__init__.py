"""Transport layer for the Fortify control plane — HTTP client + Biscuit verify.

The agent-loader equivalent (`load_fortify_agent`) lives in `fortify.agents.loader`
alongside `load_local_agent` / `load_builtin_agent`; this package only carries the
client and token verification primitives the loader uses.
"""

from fortify.cloud.biscuit import (
    TokenError,
    TokenSignatureError,
    parse_envelope,
    verify_biscuit,
)
from fortify.cloud.client import (
    DEFAULT_AGENT_NAME,
    FortifyClient,
    FortifyConfig,
    FortifyError,
    resolve_agent_name,
)

__all__ = [
    "DEFAULT_AGENT_NAME",
    "FortifyClient",
    "FortifyConfig",
    "FortifyError",
    "TokenError",
    "TokenSignatureError",
    "parse_envelope",
    "resolve_agent_name",
    "verify_biscuit",
]
