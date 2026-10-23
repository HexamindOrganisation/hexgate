"""The two discovery documents an MCP client and the MCP server fetch.

Mounted at the host root, not under ``/v1``: clients look for
``/.well-known/oauth-authorization-server`` there (RFC 8414 §3).
"""

from __future__ import annotations

from fastapi import APIRouter
from jwt.utils import base64url_encode

from hexgate_api.constants import OAUTH_SCOPES
from hexgate_api.core.oauth_tokens import ACCESS_TOKEN_ALGORITHM
from hexgate_api.features.oauth.provider import OAUTH_PREFIX
from hexgate_api.settings import get_settings

router = APIRouter()

JWKS_PATH = "/.well-known/jwks.json"


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata() -> dict[str, object]:
    """RFC 8414 metadata. Built as a plain dict, not the MCP SDK's
    ``OAuthMetadata``: its ``AnyHttpUrl`` would serialise ``issuer`` with a
    trailing slash, and ``issuer`` must equal the tokens' ``iss`` exactly."""
    issuer = get_settings().public_url
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}{OAUTH_PREFIX}/authorize",
        "token_endpoint": f"{issuer}{OAUTH_PREFIX}/token",
        "revocation_endpoint": f"{issuer}{OAUTH_PREFIX}/revoke",
        "jwks_uri": f"{issuer}{JWKS_PATH}",
        "response_types_supported": ["code"],
        # Absent, RFC 8414 adds "fragment", which only the implicit grant uses.
        "response_modes_supported": ["query"],
        # Absent, RFC 8414 means ["authorization_code", "implicit"].
        "grant_types_supported": ["authorization_code", "refresh_token"],
        # Public clients only: absent, RFC 8414 means client_secret_basic.
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": sorted(OAUTH_SCOPES),
        "client_id_metadata_document_supported": True,
    }


@router.get(JWKS_PATH)
async def jwks() -> dict[str, object]:
    """The OAuth public key only — never the SDK root key, which
    ``/v1/.well-known/keys`` serves and the SDK pins."""
    from hexgate_api.core.keystore import keystore

    return {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "use": "sig",
                # "EdDSA", not RFC 9864's "Ed25519": PyJWT drops a JWK whose
                # alg it doesn't know, and the MCP server then refuses every token.
                "alg": ACCESS_TOKEN_ALGORITHM,
                "kid": keystore.oauth_fingerprint(),
                "x": base64url_encode(keystore.oauth_public_key_bytes()).decode(),
            }
        ]
    }
