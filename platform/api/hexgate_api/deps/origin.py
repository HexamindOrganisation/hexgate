"""The dashboard ``Origin`` gate, for cookie-only POSTs a cross-site page must not send."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Header, HTTPException

from hexgate_api.settings import get_settings


def _origin(url: str) -> str | None:
    """``url``'s origin as a browser serialises it: lowercase, no default port."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    scheme = parts.scheme.lower()
    if port is None or (scheme, port) in {("http", 80), ("https", 443)}:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def require_dashboard_origin(origin: str | None = Header(default=None)) -> None:
    """Refuse a POST sent from any page but the dashboard's."""
    expected = _origin(get_settings().dashboard_url)
    # None on both sides (a malformed setting, Origin: null) is not a match.
    if expected is None or origin is None or _origin(origin) != expected:
        raise HTTPException(
            status_code=403, detail="request must come from the dashboard"
        )
