"""Tests for ``create_agent(bind_policy=...)`` (policy-binding spec, phase 3).

Covers the dispatch matrix (auto / True / False), the full bind path
(tools guarded, source attached, client attached), fail-loud on a platform
404 (no auto-register), and the ``enforce_policy`` source-detach guard that
keeps an explicit policy from being silently swapped back at the next run.

The LangChain graph build and the Langfuse handler are stubbed exactly
like tests/agents/test_factory.py does; the platform is a scripted fake
client patched over ``fortify.cloud.client.FortifyClient``.
"""

from __future__ import annotations


import pytest
from langchain_core.tools import tool

from fortify.agents import factory
from fortify.adapters.langchain.tools import GuardedTool
from fortify.cloud.client import FortifyError
from fortify.security import AgentPolicy, PolicySet
from fortify.security.policy_set import DEFAULT_ROLE_NAME
from fortify.security.source import PlatformPolicySource

_POLICY_YAML = """\
version: 1
roles:
  billing:
    tools:
      echo:
        mode: allow
"""


@tool
def echo(text: str) -> str:
    """Echo the input back."""
    return text


class _FakeClient:
    """Scripted FortifyClient stand-in (same shape as the binding tests)."""

    def __init__(self) -> None:
        self._queued: list[tuple[dict | None, str | None] | Exception] = []
        self.calls: list[str | None] = []

    def serve(self, payload: dict | None, etag: str | None = None) -> None:
        self._queued.append((payload, etag))

    def serve_error(self, exc: Exception) -> None:
        self._queued.append(exc)

    def get_agent(
        self, _name: str, *, if_none_match: str | None = None
    ) -> tuple[dict | None, str | None]:
        self.calls.append(if_none_match)
        item = self._queued.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def public_key_bytes(self) -> bytes:
        return b"\x00" * 32  # never consulted on the bundle-less path


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the graph build + handler, and clear governance env vars."""
    monkeypatch.setattr(
        factory, "create_langchain_agent", lambda **kwargs: "graph-instance"
    )
    monkeypatch.setattr(
        factory, "get_langfuse_handler", lambda **kwargs: "handler-instance"
    )
    monkeypatch.delenv("FORTIFY_KEY", raising=False)
    monkeypatch.delenv("FORTIFY_LOCAL_POLICY", raising=False)


def _patch_platform(
    monkeypatch: pytest.MonkeyPatch, client: _FakeClient
) -> None:
    """Route _bind_policy's client construction to the scripted fake."""
    import fortify.cloud.client as client_mod

    monkeypatch.setenv("FORTIFY_KEY", "fty_test_demo_dummybiscuit")
    monkeypatch.setattr(client_mod, "FortifyClient", lambda config: client)


# ---------------------------------------------------------------------------
# Dispatch matrix
# ---------------------------------------------------------------------------


def test_auto_mode_without_governance_env_returns_bare_agent() -> None:
    """No FORTIFY_KEY / FORTIFY_LOCAL_POLICY → today's bare graph."""
    agent, handler = factory.create_agent(
        model="openai:gpt-5.4", tools=[echo], name="support-bot"
    )

    assert handler == "handler-instance"
    assert agent.tools == [echo]  # unwrapped
    assert agent._binding is None


def test_auto_mode_without_name_skips_even_with_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nameless agents have nothing to resolve against — silent skip."""
    monkeypatch.setenv("FORTIFY_KEY", "fty_test_demo_dummybiscuit")

    agent, _ = factory.create_agent(model="openai:gpt-5.4", tools=[echo])

    assert agent.tools == [echo]
    assert agent._binding is None


def test_bind_policy_false_skips_even_with_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit escape hatch for bare graphs in keyed environments."""
    monkeypatch.setenv("FORTIFY_KEY", "fty_test_demo_dummybiscuit")

    agent, _ = factory.create_agent(
        model="openai:gpt-5.4", tools=[echo], name="support-bot", bind_policy=False
    )

    assert agent.tools == [echo]
    assert agent._binding is None


def test_bind_policy_true_requires_name() -> None:
    with pytest.raises(ValueError, match="requires name="):
        factory.create_agent(
            model="openai:gpt-5.4", tools=[echo], bind_policy=True
        )


# ---------------------------------------------------------------------------
# The bind path
# ---------------------------------------------------------------------------


def test_bind_wraps_tools_and_attaches_source_and_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto mode + FORTIFY_KEY + name → guarded tools, seeded source, client."""
    fc = _FakeClient()
    fc.serve({"policy_yaml": _POLICY_YAML}, etag='"hash-a"')
    _patch_platform(monkeypatch, fc)

    agent, _ = factory.create_agent(
        model="openai:gpt-5.4", tools=[echo], name="support-bot"
    )

    assert len(agent.tools) == 1
    assert isinstance(agent.tools[0], GuardedTool)
    assert isinstance(agent._binding.source, PlatformPolicySource)
    assert isinstance(agent._binding.enforcer.policy, PolicySet)
    assert agent.fortify_client is fc
    assert fc.calls == [None]  # exactly one fetch at creation


def test_bind_failure_is_loud_not_a_bare_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binding was requested (key present) — a platform error must raise,
    never silently degrade to an unguarded agent."""
    fc = _FakeClient()
    fc.serve_error(FortifyError("Fortify API error 500 calling …", status=500))
    _patch_platform(monkeypatch, fc)

    with pytest.raises(FortifyError, match="500"):
        factory.create_agent(
            model="openai:gpt-5.4", tools=[echo], name="support-bot"
        )


def test_404_is_loud_does_not_auto_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unregistered agent surfaces the 404 — create_agent never silently
    auto-creates it on the platform (register-on-404 is deferred)."""
    fc = _FakeClient()
    fc.serve_error(FortifyError("Fortify API error 404 calling …", status=404))
    _patch_platform(monkeypatch, fc)

    with pytest.raises(FortifyError) as excinfo:
        factory.create_agent(
            model="openai:gpt-5.4", tools=[echo], name="new-agent"
        )

    assert excinfo.value.status == 404
    assert fc.calls == [None]  # one fetch, no register-and-retry


def test_local_override_binds_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FORTIFY_LOCAL_POLICY alone triggers auto mode; resolve takes the
    override branch and the platform is never contacted."""
    import fortify.security.binding as binding_mod

    override_policy = PolicySet({DEFAULT_ROLE_NAME: AgentPolicy()})

    class _StubSource:
        def fetch(self) -> object:
            return override_policy

    stub = _StubSource()
    monkeypatch.setenv("FORTIFY_LOCAL_POLICY", "/tmp/override-bundle")
    monkeypatch.setattr(
        binding_mod, "_local_policy_override", lambda: (override_policy, stub)
    )

    agent, _ = factory.create_agent(
        model="openai:gpt-5.4", tools=[echo], name="support-bot"
    )

    assert isinstance(agent.tools[0], GuardedTool)
    assert agent._binding.source is stub
    assert agent.fortify_client is None


# ---------------------------------------------------------------------------
# enforce_policy detaches the inherited source
# ---------------------------------------------------------------------------


def test_enforce_policy_detaches_inherited_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit enforce_policy after binding must not be silently
    swapped back to the platform policy by the next run's refresh."""
    fc = _FakeClient()
    fc.serve({"policy_yaml": _POLICY_YAML}, etag='"hash-a"')
    _patch_platform(monkeypatch, fc)

    agent, _ = factory.create_agent(
        model="openai:gpt-5.4", tools=[echo], name="support-bot"
    )
    assert agent._binding.source is not None

    custom = agent.enforce_policy(AgentPolicy())

    assert custom._binding.source is None  # refresh can't overwrite the custom policy
    custom.refresh_policy()  # no source → quiet no-op
