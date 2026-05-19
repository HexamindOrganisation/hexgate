"""Tests for ``FortifyConfig`` and ``FortifyClient`` (SDK side).

Covers configuration resolution (explicit args / env / key prefix),
public-key sourcing precedence, and the lazy verify-before-trust flow
without making any real network calls.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from biscuit_auth import Algorithm, BiscuitBuilder, KeyPair, PrivateKey

from fortify.cloud.client import (
    FortifyClient,
    FortifyConfig,
    FortifyError,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def keys() -> tuple[bytes, bytes]:
    """Fresh Ed25519 keypair as ``(priv, pub)`` raw bytes."""
    kp = KeyPair()
    return kp.private_key.to_bytes(), kp.public_key.to_bytes()


def _envelope(
    priv: bytes,
    project: str = "support-bot",
    env: str = "live",
    *,
    extra_facts: str = "",
) -> str:
    """Mint a fully-formed ``fty_<env>_<project>_<biscuit_b64>`` envelope.

    Pass ``extra_facts='user("alice"); refund_limit(50);'`` to add
    extraction-test-friendly facts to the authority block.
    """
    pk = PrivateKey.from_bytes(priv, Algorithm.Ed25519)
    source = f'project("{project}"); env("{env}"); {extra_facts}'.strip()
    biscuit = BiscuitBuilder(source).build(pk)
    return f"fty_{env}_{project}_{biscuit.to_base64()}"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the FORTIFY_* env keys so resolution tests don't leak state."""
    for key in (
        "FORTIFY_KEY",
        "FORTIFY_API_URL",
        "FORTIFY_PROJECT_ID",
        "FORTIFY_PUBLIC_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# FortifyConfig.from_env — resolution rules
# ---------------------------------------------------------------------------


def test_config_uses_explicit_args(clean_env: None) -> None:
    """Explicit args win over env, env over key prefix."""
    config = FortifyConfig.from_env(
        api_key="fty_live_explicit-proj_abc",
        base_url="http://test.local",
        project_id="overridden",
    )
    assert config.api_key == "fty_live_explicit-proj_abc"
    assert config.base_url == "http://test.local"
    assert config.project_id == "overridden"


def test_config_strips_trailing_slash_from_base_url(clean_env: None) -> None:
    """A trailing ``/`` on FORTIFY_API_URL would double up in path concatenation."""
    config = FortifyConfig.from_env(
        api_key="fty_live_proj_secret",
        base_url="http://test.local/",
    )
    assert config.base_url == "http://test.local"


def test_config_resolves_project_from_env(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    """FORTIFY_PROJECT_ID overrides whatever the key prefix encodes."""
    monkeypatch.setenv("FORTIFY_KEY", "fty_live_key-proj_secret")
    monkeypatch.setenv("FORTIFY_PROJECT_ID", "env-proj")
    config = FortifyConfig.from_env()
    assert config.project_id == "env-proj"


def test_config_resolves_project_from_key_prefix(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    """When neither arg nor env give a project, parse it from the key prefix."""
    monkeypatch.setenv("FORTIFY_KEY", "fty_live_my-cool-project_secret")
    config = FortifyConfig.from_env()
    assert config.project_id == "my-cool-project"


def test_config_raises_when_key_missing(clean_env: None) -> None:
    """No FORTIFY_KEY is fail-fast, not silent default."""
    with pytest.raises(FortifyError, match="FORTIFY_KEY not set"):
        FortifyConfig.from_env()


def test_config_raises_when_project_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    """A key without the fty_ prefix and no FORTIFY_PROJECT_ID can't be resolved."""
    monkeypatch.setenv("FORTIFY_KEY", "completely_unparseable_key")
    with pytest.raises(FortifyError, match="Unable to resolve project id"):
        FortifyConfig.from_env()


# ---------------------------------------------------------------------------
# FortifyConfig.from_env — public key sourcing
# ---------------------------------------------------------------------------


def test_config_public_key_from_explicit_arg(
    keys: tuple[bytes, bytes],
    clean_env: None,
) -> None:
    """Explicit ``public_key=`` is preferred over env."""
    _, pub = keys
    config = FortifyConfig.from_env(
        api_key="fty_live_proj_secret",
        public_key=pub,
    )
    assert config.public_key == pub


def test_config_public_key_from_env(
    keys: tuple[bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    """FORTIFY_PUBLIC_KEY env (urlsafe-b64) decodes into raw bytes."""
    _, pub = keys
    monkeypatch.setenv("FORTIFY_KEY", "fty_live_proj_secret")
    monkeypatch.setenv("FORTIFY_PUBLIC_KEY", _b64url(pub))
    config = FortifyConfig.from_env()
    assert config.public_key == pub


def test_config_public_key_env_handles_missing_padding(
    keys: tuple[bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    """urlsafe_b64decode requires padding, but operators may strip it."""
    _, pub = keys
    monkeypatch.setenv("FORTIFY_KEY", "fty_live_proj_secret")
    encoded = base64.urlsafe_b64encode(pub).decode("ascii").rstrip("=")
    monkeypatch.setenv("FORTIFY_PUBLIC_KEY", encoded)
    config = FortifyConfig.from_env()
    assert config.public_key == pub


def test_config_invalid_pubkey_env_raises(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    """Non-base64 FORTIFY_PUBLIC_KEY is a startup-time misconfiguration."""
    monkeypatch.setenv("FORTIFY_KEY", "fty_live_proj_secret")
    monkeypatch.setenv("FORTIFY_PUBLIC_KEY", "!!!not-base64!!!")
    with pytest.raises(FortifyError, match="not valid base64"):
        FortifyConfig.from_env()


def test_config_no_public_key_when_neither_set(clean_env: None) -> None:
    """Without explicit arg or env var, public_key is None — JWKS fetch later."""
    config = FortifyConfig.from_env(api_key="fty_live_proj_secret")
    assert config.public_key is None


# ---------------------------------------------------------------------------
# FortifyClient — lazy verify on first call
# ---------------------------------------------------------------------------


def _stub_get_agent_response(name: str = "default") -> dict[str, Any]:
    return {
        "id": "agt_test",
        "name": name,
        "agent_yaml": "name: default\n",
        "policy_yaml": "version: 1\n",
        "system_md": "",
        "updated_at": "2026-05-06T00:00:00Z",
    }


def test_client_verifies_on_first_call_then_caches(
    keys: tuple[bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First call sets verified=True; second call short-circuits."""
    priv, pub = keys
    config = FortifyConfig(
        base_url="http://test",
        api_key=_envelope(priv),
        project_id="support-bot",
        public_key=pub,
    )
    client = FortifyClient(config)

    calls: list[tuple[str, bool]] = []

    def fake_raw_get(self: FortifyClient, url: str, *, authorize: bool) -> dict[str, Any]:
        calls.append((url, authorize))
        return _stub_get_agent_response()

    monkeypatch.setattr(FortifyClient, "_raw_get", fake_raw_get)

    assert not client._verified
    client.get_agent("default")
    assert client._verified
    assert len(calls) == 1
    assert calls[0][1] is True  # authorized

    client.get_agent("default")
    assert len(calls) == 2  # no extra verify call


def test_client_rejects_tampered_key_before_any_http(
    keys: tuple[bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tampered envelope fails the sig check; no HTTP request is sent."""
    priv, pub = keys
    tampered = _envelope(priv)[:-4] + "AAAA"
    config = FortifyConfig(
        base_url="http://test",
        api_key=tampered,
        project_id="support-bot",
        public_key=pub,
    )
    client = FortifyClient(config)

    def must_not_be_called(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("HTTP request fired despite signature failure")

    monkeypatch.setattr(FortifyClient, "_raw_get", must_not_be_called)

    with pytest.raises(FortifyError, match="signature does not chain"):
        client.get_agent("default")


def test_client_rejects_malformed_envelope(
    keys: tuple[bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-fty envelope surfaces a clear FortifyError."""
    _, pub = keys
    config = FortifyConfig(
        base_url="http://test",
        api_key="not_an_envelope_at_all",
        project_id="support-bot",
        public_key=pub,
    )
    client = FortifyClient(config)

    monkeypatch.setattr(
        FortifyClient,
        "_raw_get",
        lambda *a, **kw: pytest.fail("HTTP fired despite malformed key"),
    )

    with pytest.raises(FortifyError, match="malformed"):
        client.get_agent("default")


def test_client_fetches_jwks_when_pubkey_unset(
    keys: tuple[bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a cached pubkey, the client fetches /v1/.well-known/keys first."""
    priv, pub = keys
    config = FortifyConfig(
        base_url="http://test",
        api_key=_envelope(priv),
        project_id="support-bot",
        public_key=None,  # forces JWKS fetch
    )
    client = FortifyClient(config)

    calls: list[tuple[str, bool]] = []

    def fake_raw_get(self: FortifyClient, url: str, *, authorize: bool) -> dict[str, Any]:
        calls.append((url, authorize))
        if url.endswith("/.well-known/keys"):
            return {"keys": [{"x": _b64url(pub), "fingerprint": "sha256:abcdef0123456789"}]}
        return _stub_get_agent_response()

    monkeypatch.setattr(FortifyClient, "_raw_get", fake_raw_get)

    client.get_agent("default")

    # JWKS first (unauthorized), then agent fetch (authorized).
    assert len(calls) == 2
    assert calls[0][0].endswith("/v1/.well-known/keys")
    assert calls[0][1] is False
    assert "/v1/projects/support-bot/agents/default" in calls[1][0]
    assert calls[1][1] is True


def test_client_jwks_response_with_unexpected_shape_raises(
    keys: tuple[bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JWKS response missing 'keys' or 'x' surfaces a clear error."""
    priv, _ = keys
    config = FortifyConfig(
        base_url="http://test",
        api_key=_envelope(priv),
        project_id="support-bot",
        public_key=None,
    )
    client = FortifyClient(config)

    monkeypatch.setattr(
        FortifyClient,
        "_raw_get",
        lambda self, url, *, authorize: {"unexpected": "shape"},
    )

    with pytest.raises(FortifyError, match="unexpected JWKS shape"):
        client.get_agent("default")


# ---------------------------------------------------------------------------
# FortifyClient.biscuit_facts() — fact extraction behind the verify gate
# ---------------------------------------------------------------------------


def test_client_biscuit_facts_returns_token_facts(
    keys: tuple[bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """biscuit_facts() runs the verify gate and returns the cached facts."""
    priv, pub = keys
    config = FortifyConfig(
        base_url="http://test",
        api_key=_envelope(
            priv, extra_facts='user("alice"); refund_limit(50); scope("refund");'
        ),
        project_id="support-bot",
        public_key=pub,
    )
    client = FortifyClient(config)

    facts = client.biscuit_facts()
    # project + env are stamped by _envelope itself
    assert facts["user"] == ["alice"]
    assert facts["refund_limit"] == [50]
    assert facts["scope"] == ["refund"]
    assert facts["project"] == ["support-bot"]


def test_client_biscuit_facts_runs_verify_gate(
    keys: tuple[bytes, bytes],
) -> None:
    """A tampered token must surface as FortifyError, not return partial facts."""
    priv, pub = keys
    tampered = _envelope(priv)[:-4] + "AAAA"
    config = FortifyConfig(
        base_url="http://test",
        api_key=tampered,
        project_id="support-bot",
        public_key=pub,
    )
    client = FortifyClient(config)

    with pytest.raises(FortifyError, match="signature does not chain"):
        client.biscuit_facts()


def test_client_biscuit_facts_returns_independent_copy(
    keys: tuple[bytes, bytes],
) -> None:
    """Mutating the returned dict must not poison the cached extraction."""
    priv, pub = keys
    config = FortifyConfig(
        base_url="http://test",
        api_key=_envelope(priv, extra_facts='user("alice");'),
        project_id="support-bot",
        public_key=pub,
    )
    client = FortifyClient(config)

    first = client.biscuit_facts()
    first["user"].append("bob")
    second = client.biscuit_facts()
    assert second["user"] == ["alice"]
