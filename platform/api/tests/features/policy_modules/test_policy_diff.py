"""POST /policy/diff and the comparison behind it."""

from __future__ import annotations

import pytest

from hexgate_api.features.policy_modules.diff import _resolve, diff_files
from tests.features.policy_modules.test_policy_modules import _project, _put_file

# Two agents share the entry's top-level boundary and one imported grants file.
_SHARED = {
    "policy.yaml": (
        "boundary: { tools: { refund: {} } }\n"
        "agents:\n"
        "  bot:\n"
        "    roles:\n"
        "      support: { import: [ grants.yaml ] }\n"
        "  helper:\n"
        "    roles:\n"
        "      support: { import: [ grants.yaml ] }\n"
    ),
    "grants.yaml": "tools: { refund: { mode: allow } }\n",
}


def _entry(body: str) -> dict[str, str]:
    return {"policy.yaml": "agents:\n  bot:\n    roles:\n      support:\n" + body}


def test_diff_files_happy_path() -> None:
    saved = _entry("        tools: { a: { mode: allow }, b: { mode: allow } }\n")
    drafted = _entry(
        "        tools:\n"
        "          a: { mode: approval_required }\n"
        "          c: { mode: allow, constraint: 'args.n <= 5' }\n"
    )
    out = diff_files(saved, drafted)
    assert {(c["agent"], c["role"]) for c in out["changes"]} == {("bot", "support")}
    by_tool = {c["tool"]: c for c in out["changes"]}
    assert set(by_tool) == {"a", "b", "c"}
    assert by_tool["a"]["before"] == {"mode": "allow", "constraints": []}
    assert by_tool["a"]["after"]["mode"] == "approval_required"
    assert by_tool["b"]["after"] is None  # no longer reachable
    assert by_tool["c"]["before"] is None
    assert by_tool["c"]["constraints_added"] == ["(args.n <= 5)"]  # SDK-normalized
    assert out["agents_affected"] == ["bot"]
    assert out["composes"] == {"before": True, "after": True}
    assert out["agent_changes"] == []


def test_when_files_unchanged_then_diff_is_empty() -> None:
    out = diff_files(_SHARED, dict(_SHARED))
    assert out["changes"] == [] and out["agents_affected"] == []


def test_when_shared_boundary_tightens_then_both_agents_change() -> None:
    drafted = {
        **_SHARED,
        "policy.yaml": _SHARED["policy.yaml"].replace(
            "refund: {}", "refund: { constraint: 'args.amount <= 50' }"
        ),
    }
    out = diff_files(_SHARED, drafted)
    assert out["agents_affected"] == ["bot", "helper"]
    for change in out["changes"]:
        assert (change["role"], change["tool"]) == ("support", "refund")
        assert change["constraints_added"] == ["args.amount <= 50"]
        assert change["constraints_removed"] == []


def test_when_two_files_drafted_then_both_apply() -> None:
    # The grants file adds a tool and the entry's boundary admits it: the diff
    # needs both drafts to see the new tool reachable.
    drafted = {
        "policy.yaml": _SHARED["policy.yaml"].replace(
            "refund: {}", "refund: {}, lookup: {}"
        ),
        "grants.yaml": "tools: { refund: { mode: allow }, lookup: { mode: allow } }\n",
    }
    out = diff_files(_SHARED, drafted)
    assert {(c["agent"], c["tool"]) for c in out["changes"]} == {
        ("bot", "lookup"),
        ("helper", "lookup"),
    }
    one_file = {**_SHARED, "grants.yaml": drafted["grants.yaml"]}
    assert diff_files(_SHARED, one_file)["changes"] == []  # the boundary caps lookup


def test_when_both_vias_change_alike_then_reach_collapses() -> None:
    # Reach is declared on both sides, so the gate is on and only ops_bot moves.
    saved = _entry("        reach: { billing_bot: {} }\n")
    drafted = _entry("        reach: { billing_bot: {}, ops_bot: {} }\n")
    out = diff_files(saved, drafted)
    support = [c for c in out["changes"] if c["role"] == "support"]
    assert [c["tool"] for c in support] == ["agent:ops_bot"]
    assert support[0]["before"] is None
    assert support[0]["after"]["mode"] == "allow"


def test_when_reach_is_first_declared_then_other_roles_lose_it() -> None:
    # Undeclared reach is open to every role; declaring it closes the rest.
    saved = _entry("        tools: { a: { mode: allow } }\n")
    drafted = _entry("        reach: { billing_bot: {} }\n")
    drafted["policy.yaml"] = (
        "boundary: { reach: { billing_bot: {} } }\n" + drafted["policy.yaml"]
    )
    out = diff_files(saved, drafted)
    by_role = {
        c["role"]: c
        for c in out["changes"]
        if c["agent"] == "bot" and c["tool"].startswith("agent")
    }
    # The as-tool via moved to its own gate (it has no per-key value before).
    assert by_role["default"]["tool"] == "agent.handoff:billing_bot"
    assert by_role["default"]["before"]["mode"] == "allow"
    assert by_role["default"]["after"] is None
    assert "support" not in by_role  # open before, granted after
    gates = {g["gate"] for g in out["gate_changes"] if g["agent"] == "bot"}
    assert gates == {"reach", "tool_reach"}


def test_when_vias_differ_then_reach_splits_per_via() -> None:
    saved = _entry("        reach: { billing_bot: {} }\n")
    drafted = _entry("        reach: { billing_bot: { as: tool } }\n")
    out = diff_files(saved, drafted)
    assert [c["tool"] for c in out["changes"]] == ["agent.handoff:billing_bot"]
    assert out["changes"][0]["after"] is None


def test_when_admission_is_first_declared_then_other_roles_lose_entry() -> None:
    saved = _entry("        tools: { a: { mode: allow } }\n")
    drafted = _entry("        tools: { a: { mode: allow } }\n        admission: {}\n")
    # Undeclared admission lets everyone in; declaring it for support shuts
    # out default, the only role that loses entry.
    rows = [
        (c["role"], c["before"], c["after"])
        for c in diff_files(saved, drafted)["changes"]
        if c["agent"] == "bot"
    ]
    assert rows == [("default", {"mode": "allow", "constraints": []}, None)]


def test_when_only_usage_flips_then_one_agent_change_per_agent() -> None:
    body = "        tools: { a: { mode: allow } }\n"
    saved = _entry(body)
    drafted = {
        "policy.yaml": "usage: { on_unavailable: deny }\n" + saved["policy.yaml"]
    }
    out = diff_files(saved, drafted)
    assert out["changes"] == []
    # Top-level usage reaches "*" (every undeclared agent) and bot, once each,
    # though bot has two roles: the value is agent-level.
    assert out["agent_changes"] == [
        {
            "agent": agent,
            "key": "usage",
            "before": None,
            "after": {"on_unavailable": "deny"},
        }
        for agent in ("*", "bot")
    ]
    assert out["agents_affected"] == ["*", "bot"]


def test_when_draft_does_not_compose_then_composes_after_is_false() -> None:
    out = diff_files(_SHARED, {**_SHARED, "policy.yaml": "import: [ nope.yaml ]\n"})
    assert out["composes"] == {"before": True, "after": False}
    assert out["changes"] == []


def test_when_agent_guards_change_then_one_agent_change() -> None:
    body = "        tools: { a: { mode: allow } }\n"
    saved = _entry(body)
    drafted = {
        "policy.yaml": saved["policy.yaml"].replace(
            "  bot:\n", "  bot:\n    guards: { secret_guard: { enabled: false } }\n"
        )
    }
    out = diff_files(saved, drafted)
    assert [(c["agent"], c["key"]) for c in out["agent_changes"]] == [("bot", "guards")]
    assert out["agent_changes"][0]["after"] == {"secret_guard": {"enabled": False}}


def test_every_role_carries_its_agents_guards_and_usage() -> None:
    # agent_changes reads guards and usage from the default role only, which is
    # complete because compose puts the agent's one value on every role.
    entry = _SHARED["policy.yaml"].replace(
        "  bot:\n", "  bot:\n    guards: { secret_guard: { enabled: false } }\n"
    )
    files = {**_SHARED, "policy.yaml": "usage: { on_unavailable: deny }\n" + entry}
    for result in _resolve(files).values():
        policies = [result.policy_set.policy_for(r) for r in result.policy_set.roles]
        assert len({(p.usage, str(p.guards)) for p in policies}) == 1


def test_resolved_default_policy_is_always_deny() -> None:
    # The diff compares listed keys only, which is complete because no draft can
    # change what an unlisted tool falls to. If this ever fails, the diff misses
    # default changes.
    from hexgate.security import DEFAULT_ROLE_NAME

    for result in _resolve(_SHARED).values():
        for link in result.by_role.values():
            policy = link.effective[DEFAULT_ROLE_NAME]
            assert policy.default_policy.mode == "deny"


def test_api_diff_policy_happy_path(client) -> None:
    pid = _project(client)
    for name, content in reversed(_SHARED.items()):  # grants first: entry imports it
        assert _put_file(client, pid, name, content).status_code == 200, name
    r = client.post(
        f"/v1/projects/{pid}/policy/diff",
        json={"drafts": [{"name": "grants.yaml", "content": "tools: {}\n"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agents_affected"] == ["bot", "helper"]
    assert {c["tool"] for c in body["changes"]} == {"refund"}
    assert all(c["after"] is None for c in body["changes"])
    # nothing was saved
    files = {
        f["name"]: f["content"]
        for f in client.get(f"/v1/projects/{pid}/policy-files").json()
    }
    assert files["grants.yaml"] == _SHARED["grants.yaml"]


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        # No saved policy.yaml: nothing to compare, even with a first one drafted.
        (
            {"drafts": [{"name": "policy.yaml", "content": "tools: {}\n"}]},
            "nothing to diff",
        ),
        ({"drafts": []}, "at least 1 item"),
        (
            {"drafts": [{"name": "a.yaml", "content": ""}], "agent": "bot"},
            "Extra inputs",
        ),
    ],
)
def test_when_body_cannot_be_diffed_then_422(client, body, detail) -> None:
    pid = _project(client)
    r = client.post(f"/v1/projects/{pid}/policy/diff", json=body)
    assert r.status_code == 422
    assert detail in str(r.json()["detail"])


_ROLES = "tools: {{ a: {{ mode: allow }} }}\nagents:\n  bot:\n    roles:\n{roles}"


def _roles(roles: str) -> dict[str, str]:
    return {"policy.yaml": _ROLES.format(roles=roles)}


def test_when_role_is_removed_then_it_falls_back_to_default() -> None:
    # A caller in the removed role gets bot's `default`, which still allows `a`.
    saved = _roles("      support: { tools: { b: { mode: allow } } }\n")
    out = diff_files(saved, _roles("      guest: { tools: { c: { mode: allow } } }\n"))
    support = {c["tool"]: c for c in out["changes"] if c["role"] == "support"}
    assert set(support) == {"b"}  # `a` still reachable through `default`


def test_when_role_is_added_then_lost_default_grants_show() -> None:
    saved = _roles("      default: { tools: { x: { mode: allow } } }\n")
    drafted = _roles(
        "      default: { tools: { x: { mode: allow } } }\n"
        "      auditor: { tools: { y: { mode: allow } } }\n"
    )
    changes = diff_files(saved, drafted)["changes"]
    assert {(c["agent"], c["role"]) for c in changes} == {("bot", "auditor")}
    auditor = {c["tool"]: c for c in changes}
    assert auditor["x"]["after"] is None  # auditor no longer falls back to default
    assert auditor["y"]["before"] is None


def test_when_admission_is_removed_then_every_role_is_admitted() -> None:
    # With no role declaring admission the gate is off: guest gains entry too.
    saved = _roles(
        "      support: { admission: {} }\n      guest: { tools: { b: { mode: allow } } }\n"
    )
    drafted = _roles("      guest: { tools: { b: { mode: allow } } }\n")
    runs = {
        c["role"]: c
        for c in diff_files(saved, drafted)["changes"]
        if c["tool"] == "agent.run" and c["agent"] == "bot"
    }
    assert runs["guest"]["before"] is None
    assert runs["guest"]["after"]["mode"] == "allow"
    assert "support" not in runs  # admitted before and after


def test_when_handoff_reach_is_first_declared_then_reach_gate_flips() -> None:
    # Undeclared handoff is open to every role; declaring it for support closes
    # it for default. The as-tool seam stays on tool names: no row, no flip.
    saved = _entry("        tools: { a: { mode: allow } }\n")
    drafted = _entry("        reach: { billing_bot: { as: handoff } }\n")
    drafted["policy.yaml"] = (
        "boundary: { reach: { billing_bot: { as: handoff } } }\n"
        + drafted["policy.yaml"]
    )
    out = diff_files(saved, drafted)
    rows = {
        (c["role"], c["tool"])
        for c in out["changes"]
        if c["agent"] == "bot" and c["tool"].startswith("agent")
    }
    assert rows == {("default", "agent.handoff:billing_bot")}
    gates = {(g["agent"], g["gate"]) for g in out["gate_changes"]}
    assert ("bot", "reach") in gates and ("bot", "tool_reach") not in gates


def test_when_first_skill_is_granted_then_skills_gate_flips() -> None:
    # Before, skill reads go through read_file; after, every role but support
    # loses them. That shows as the gate, not as skill rows.
    body = "        tools: { read_file: { mode: allow } }\n"
    saved = _entry(body)
    drafted = _entry(body + "        skills: { refunder: {} }\n")
    drafted["policy.yaml"] = (
        "boundary: { tools: { read_file: {} }, skills: { refunder: {} } }\n"
        + drafted["policy.yaml"]
    )
    saved["policy.yaml"] = (
        "boundary: { tools: { read_file: {} } }\n" + saved["policy.yaml"]
    )
    out = diff_files(saved, drafted)
    assert [c for c in out["changes"] if c["tool"].startswith("skill")] == []
    assert {"agent": "bot", "gate": "skills", "before": False, "after": True} in out[
        "gate_changes"
    ]
    assert "bot" in out["agents_affected"]


def test_when_first_as_tool_reach_is_declared_then_tool_reach_gate_flips() -> None:
    saved = _entry("        reach: { billing_bot: { as: handoff } }\n")
    drafted = _entry("        reach: { billing_bot: {} }\n")
    # A boundary listing a via declares it too (as a deny), so it moves with it.
    for files, via in ((saved, "{ as: handoff }"), (drafted, "{}")):
        files["policy.yaml"] = (
            f"boundary: {{ reach: {{ billing_bot: {via} }} }}\n" + files["policy.yaml"]
        )
    out = diff_files(saved, drafted)
    assert [c for c in out["changes"] if c["tool"].startswith("agent.tool")] == []
    assert {g["gate"] for g in out["gate_changes"] if g["agent"] == "bot"} == {
        "tool_reach"
    }
