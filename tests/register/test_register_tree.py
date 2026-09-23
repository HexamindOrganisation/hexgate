"""Tests for ``register_tree`` — recursive, memoized, idempotent tree registration.

Drives the walk with a stubbed ``post_manifest`` (no network): a fake child tree of
``HexgateAgent``s built from trivial graphs, so ``create_manifest`` +
``enumerate_subagents`` see real sub-agent edges. Asserts post-order, cycle/shared-node
memoization, and fail-loud-on-collision (with ``force`` override).
"""

from __future__ import annotations

import pytest

from hexgate.cli.register import register as register_mod
from hexgate.cli.register.register import AgentTreeCollision, register_tree


def _graph(name: str):
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(dict)
    builder.add_node("noop", lambda state: state)
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    return builder.compile(name=name)


def _hg(name: str, *, subs=(), model: str = "m"):
    """A HexgateAgent with a trivial graph and mounted agent-as-tool sub-agents."""
    from hexgate.agents.factory import HexgateAgent

    return HexgateAgent(
        graph=_graph(name),
        model=model,
        tools=[s.as_tool() for s in subs],
        system_prompt=None,
        name=name,
    )


@pytest.fixture()
def _posts(monkeypatch: pytest.MonkeyPatch):
    """Stub ``post_manifest``; record (name) in call order, return a created result."""
    calls: list[str] = []

    def fake_post(manifest, *, timeout=None):
        calls.append(manifest.name)
        return {"created": True, "version": 1, "content_hash": "h"}

    monkeypatch.setattr(register_mod, "post_manifest", fake_post)
    return calls


def test_registers_children_before_parent(_posts) -> None:
    child = _hg("billing_bot")
    parent = _hg("support_bot", subs=[child])
    register_tree(parent)
    assert _posts == ["billing_bot", "support_bot"]  # post-order


def test_deep_tree_registers_every_level_once(_posts) -> None:
    grandchild = _hg("gc")
    child = _hg("child", subs=[grandchild])
    parent = _hg("parent", subs=[child])
    register_tree(parent)
    assert _posts == ["gc", "child", "parent"]  # to any depth, post-order


def test_shared_node_registered_once(_posts) -> None:
    shared = _hg("shared")
    a = _hg("a", subs=[shared])
    b = _hg("b", subs=[shared])
    root = _hg("root", subs=[a, b])
    register_tree(root)
    assert _posts.count("shared") == 1  # diamond → visited once (memoized)
    assert _posts[-1] == "root"
    assert _posts.index("shared") < _posts.index("a")


def test_collision_same_name_different_manifest_raises(_posts) -> None:
    dup_a = _hg("dup", model="model-a")
    dup_b = _hg("dup", model="model-b")  # same name, different manifest
    parent = _hg("parent", subs=[dup_a, dup_b])
    with pytest.raises(AgentTreeCollision, match="two different agents named 'dup'"):
        register_tree(parent)


def test_collision_is_overridable_with_force(_posts, caplog) -> None:
    import logging

    dup_a = _hg("dup", model="model-a")
    dup_b = _hg("dup", model="model-b")
    parent = _hg("parent", subs=[dup_a, dup_b])
    with caplog.at_level(logging.WARNING):
        register_tree(parent, force=True)  # does not raise
    assert _posts.count("dup") == 1  # first wins; second skipped
    assert "parent" in _posts
    assert "skipping agent 'dup'" in caplog.text  # the drop is not silent


def test_force_skip_still_registers_the_skipped_nodes_subtree(_posts) -> None:
    # dup_a and dup_b collide (same name, different manifest); dup_b is force-skipped.
    # But dup_b's own distinct sub-agent (helper2) is a real, unrelated agent — it must
    # still be registered, not silently dropped along with the skipped colliding node.
    dup_a = _hg("dup", model="model-a", subs=[_hg("helper1")])
    dup_b = _hg("dup", model="model-b", subs=[_hg("helper2")])
    parent = _hg("parent", subs=[dup_a, dup_b])
    register_tree(parent, force=True)
    assert _posts.count("dup") == 1  # colliding node itself still skipped
    assert "helper1" in _posts and "helper2" in _posts  # both subtrees registered


def test_cycle_back_to_root_with_override_is_not_a_collision(_posts) -> None:
    # A cycle to the root (or a shared node == root) must be recognized by object
    # identity, so a root-only override (description) doesn't make the rebuilt revisit
    # dump differ and trigger a spurious collision on a perfectly valid tree.
    root = _hg("root")
    child = _hg("child", subs=[root])
    root.tools.append(child.as_tool())  # root -> child -> root
    register_tree(root, description="an override only the root gets")
    assert _posts == ["child", "root"]
    assert _posts.count("root") == 1


def test_same_manifest_twice_is_idempotent_not_a_collision(_posts) -> None:
    # Two *identical* same-named children (same manifest) dedup, not collide.
    root = _hg("root", subs=[_hg("twin"), _hg("twin")])
    register_tree(root)
    assert _posts.count("twin") == 1
    assert _posts[-1] == "root"


def test_on_register_fires_per_posted_node(_posts) -> None:
    seen: list[str] = []
    parent = _hg("support_bot", subs=[_hg("billing_bot")])
    register_tree(parent, on_register=lambda name, result: seen.append(name))
    assert seen == ["billing_bot", "support_bot"]


def test_cli_register_flag_selects_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """`hexgate register --register-subagents` routes to register_tree; else single."""
    import argparse
    import importlib

    import hexgate.bootstrap as boot

    # The register package re-exports the ``main`` function, shadowing the ``main``
    # submodule attribute — reach the module through importlib, not attribute access.
    main_mod = importlib.import_module("hexgate.cli.register.main")

    calls: list[str] = []
    monkeypatch.setattr(boot, "bootstrap", lambda: None)
    monkeypatch.setattr(main_mod, "_load_agent", lambda spec: object())
    monkeypatch.setattr(main_mod, "register_tree", lambda *a, **k: calls.append("tree"))
    monkeypatch.setattr(
        main_mod, "register_agent", lambda *a, **k: calls.append("single")
    )

    def _ns(register_subagents: bool) -> argparse.Namespace:
        return argparse.Namespace(
            agent="x:y",
            description=None,
            tools=None,
            model=None,
            system_prompt=None,
            register_subagents=register_subagents,
            force=False,
        )

    assert main_mod.main(_ns(True)) == 0
    assert calls == ["tree"]
    calls.clear()
    assert main_mod.main(_ns(False)) == 0
    assert calls == ["single"]


def test_root_manifest_is_reused_when_provided(
    _posts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing manifest= skips rebuilding the root (serve already has it)."""
    root = _hg("root", subs=[_hg("child")])
    prebuilt = register_mod.create_manifest(root)
    calls = {"n": 0}
    real = register_mod.create_manifest

    def counting(agent, **kwargs):
        calls["n"] += 1
        return real(agent, **kwargs)

    monkeypatch.setattr(register_mod, "create_manifest", counting)
    register_tree(root, manifest=prebuilt)
    assert calls["n"] == 1  # only the child is built; the root manifest is reused
    assert _posts == ["child", "root"]
