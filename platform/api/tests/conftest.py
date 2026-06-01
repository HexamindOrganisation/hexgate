"""Pytest config shared across the platform-api test suite.

Enables the ``X-Dev-User`` test seam by default — production servers
leave ``FORTIFY_ALLOW_DEV_USER_HEADER`` unset, but every test in this
package uses the header to assert tenant-isolation and route gating
without going through a real cookie login. The seam is gated in
``main.require_user`` / ``main.require_user_or_sdk_token`` so a
deployment that forgets to disable it doesn't accidentally expose
identity-asserting headers.
"""

from __future__ import annotations

import os


def pytest_configure(config) -> None:  # noqa: ARG001 — pytest hook
    """Flip the X-Dev-User gate on for the entire test session.

    Set early (in pytest_configure, not a fixture) so route-mounting
    code that reads the env at import time sees it. Tests that need
    to verify the prod-safe disabled behaviour can monkeypatch.delenv
    inside the test body.
    """
    os.environ.setdefault("FORTIFY_ALLOW_DEV_USER_HEADER", "1")
