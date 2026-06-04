"""Transport layer for the Fortify control plane — HTTP client + Biscuit verify.

The agent-loader equivalent (`load_fortify_agent`) lives in `fortify.agents.loader`
alongside `load_local_agent` / `load_builtin_agent`; this package only carries the
client and token verification primitives the loader uses.
"""

from fortify.cloud.attenuate import attenuate_for_user
from fortify.cloud.biscuit import (
    TokenError,
    TokenSignatureError,
    extract_facts,
    parse_envelope,
    verify_biscuit,
)
from fortify.cloud.client import (
    FortifyClient,
    FortifyConfig,
    FortifyError,
)

__all__ = [
    "FortifyClient",
    "FortifyConfig",
    "FortifyError",
    "TokenError",
    "TokenSignatureError",
    "attenuate_for_user",
    "extract_facts",
    "parse_envelope",
    "verify_biscuit",
]
