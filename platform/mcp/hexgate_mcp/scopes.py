"""The OAuth scope registry, copied from the API.

Copied rather than imported: this package never imports ``hexgate_api``.
tests/test_scopes.py fails when the two lists drift.
"""

# platform/api/hexgate_api/constants.py OAUTH_SCOPES
OAUTH_SCOPES = frozenset(
    {"policy:read", "policy:write", "audit:read", "agents:read", "projects:read"}
)
