"""Verify and reset email links take their base from ``Settings.dashboard_url``."""

from __future__ import annotations

import pytest

from hexgate_api.settings import Settings


def test_when_dashboard_url_set_then_email_link_base_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hexgate_api.features.auth import service

    settings = Settings(
        _env_file=None,
        dashboard_url="https://app.hexgate.ai/",
        oauth_allow_insecure_client_ids=False,
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    assert service._dashboard_url() == "https://app.hexgate.ai"
