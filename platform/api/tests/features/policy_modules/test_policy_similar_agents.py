"""Tests for GET /projects/{id}/policy/similar-agents: the registered agents
closest to one, by keyword, each with the reasons it ranked."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.features.policy_modules.conftest import _project, _put_file
from hexgate_api.features.policy_modules import similar

ENTRY = (
    "agents:\n"
    "  refund_bot:\n"
    "    roles:\n"
    "      support:\n"
    "        tools: { issue_refund: { mode: allow } }\n"
    "  billing_bot:\n"
    "    roles:\n"
    "      support:\n"
    "        tools: { issue_refund: { mode: approval_required } }\n"
    "      finance:\n"
    "        tools: { export_invoices: { mode: allow } }\n"
)


def _similar(client: TestClient, pid: str, agent: str, **params):
    return client.get(
        f"/v1/projects/{pid}/policy/similar-agents", params={"agent": agent, **params}
    )


async def test_policy_similar_agents_happy_path(client, register, add_bare_agent):
    pid = _project(client)
    assert _put_file(client, pid, "policy.yaml", ENTRY).status_code == 200
    await register(pid, "refund_bot", ["issue_refund", "lookup_order"])
    await register(pid, "billing_bot", ["issue_refund", "export_invoices"])
    await register(pid, "weather_bot", ["get_forecast"])
    await add_bare_agent(pid, "draft_bot")

    r = _similar(client, pid, "refund_bot")

    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["agent"], body["has_manifest"]) == ("refund_bot", True)
    # weather_bot shares nothing; draft_bot has no manifest and enforces "*".
    assert [m["agent"] for m in body["matches"]] == ["billing_bot"]
    match = body["matches"][0]
    assert (match["agent"], match["has_policy"]) == ("billing_bot", True)
    assert match["roles"] == ["finance", "support"]
    assert match["reasons"] == {
        "shared_tools": ["issue_refund"],
        "shared_name_words": ["issue", "refund"],
        "shared_roles": ["support"],
    }
    # One shared tool of three in the union, the rarer ones weighing more.
    rare = similar.rarity
    assert match["signals"]["tools"] == pytest.approx(
        rare(2, 4) / (rare(2, 4) + 2 * rare(1, 4))
    )
    assert match["score"] == pytest.approx(
        similar.WEIGHT_TOOLS * match["signals"]["tools"]
        + similar.WEIGHT_NAME_WORDS * match["signals"]["name_words"]
        + similar.WEIGHT_ROLES * match["signals"]["roles"],
        abs=1e-3,
    )


async def test_when_agents_share_only_name_words_then_the_words_are_the_reason(
    client, register
):
    pid = _project(client)
    await register(pid, "a", ["send_email"])
    await register(pid, "b", ["send_slack"])
    await register(pid, "c", ["email_draft"])

    matches = _similar(client, pid, "a").json()["matches"]

    assert [m["agent"] for m in matches] == ["b", "c"]
    assert matches[0]["reasons"]["shared_name_words"] == ["send"]
    assert matches[1]["reasons"]["shared_name_words"] == ["email"]
    assert matches[0]["signals"]["tools"] == 0
    assert 0 < matches[0]["signals"]["name_words"] < 1


async def test_when_several_agents_match_then_best_first_ties_by_name_and_limited(
    client, register
):
    pid = _project(client)
    await register(pid, "target", ["read_ticket", "close_ticket"])
    await register(pid, "zed", ["read_ticket"])
    await register(pid, "amy", ["read_ticket"])
    await register(pid, "best", ["read_ticket", "close_ticket"])

    names = [m["agent"] for m in _similar(client, pid, "target").json()["matches"]]
    limited = _similar(client, pid, "target", limit=2).json()["matches"]

    assert names == ["best", "amy", "zed"]
    assert [m["agent"] for m in limited] == ["best", "amy"]


async def test_when_target_has_no_manifest_then_only_shared_roles_match(
    client, register, add_bare_agent
):
    pid = _project(client)
    assert _put_file(client, pid, "policy.yaml", ENTRY).status_code == 200
    await add_bare_agent(pid, "refund_bot")
    await register(pid, "billing_bot", ["issue_refund"])

    body = _similar(client, pid, "refund_bot").json()

    assert body["has_manifest"] is False
    assert body["matches"][0]["reasons"]["shared_roles"] == ["support"]
    assert body["matches"][0]["reasons"]["shared_tools"] == []


async def test_when_project_has_no_policy_store_then_own_policies_are_compared(
    client, register
):
    pid = _project(client)
    await register(pid, "a", ["read_ticket"])
    await register(pid, "b", ["read_ticket"])

    matches = _similar(client, pid, "a").json()["matches"]

    assert [(m["agent"], m["has_policy"]) for m in matches] == [("b", True)]


async def test_when_agent_is_not_registered_then_404(client, register):
    pid = _project(client)
    await register(pid, "a", ["read_ticket"])

    r = _similar(client, pid, "ghost")

    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


def test_when_project_does_not_compose_then_422(client, monkeypatch):
    from hexgate.security import LinkError

    from hexgate_api.features.policy_modules import service

    pid = _project(client)
    assert _put_file(client, pid, "policy.yaml", ENTRY).status_code == 200

    async def fail(*_):
        raise LinkError("cannot import nope.yaml")

    monkeypatch.setattr(service, "resolve_every_agent", fail)

    r = _similar(client, pid, "refund_bot")

    assert r.status_code == 422
    assert "nope.yaml" in r.json()["detail"]


async def test_when_project_has_no_policy_then_roles_are_not_a_reason(client, register):
    pid = _project(client)
    await register(pid, "a", ["read_ticket"])
    await register(pid, "b", ["merge_pr"])

    # A classic project seeds every agent's own policy with the same roles.
    assert _similar(client, pid, "a").json()["matches"] == []


async def test_when_agent_param_is_missing_then_422(client):
    pid = _project(client)

    r = client.get(f"/v1/projects/{pid}/policy/similar-agents")

    assert r.status_code == 422


async def test_when_names_share_only_short_verbs_then_they_do_not_match(
    client, register
):
    pid = _project(client)
    await register(pid, "a", ["get_order"])
    await register(pid, "b", ["get_weather"])

    assert _similar(client, pid, "a").json()["matches"] == []


async def test_when_agents_have_no_policy_section_then_the_generic_roles_do_not_match(
    client, register
):
    pid = _project(client)
    entry = (
        "agents:\n  other:\n    roles:\n      support:\n"
        "        tools: { read: { mode: allow } }\n"
    )
    assert _put_file(client, pid, "policy.yaml", entry).status_code == 200
    await register(pid, "a", ["read_x"])
    await register(pid, "b", ["write_y"])

    # Neither is declared, so both run under "*" and share its roles.
    assert _similar(client, pid, "a").json()["matches"] == []


def _letters(i: int) -> str:
    """``aa``, ``ab``, ... a distinct pair of letters per ``i`` below 676."""
    return chr(97 + i // 26) + chr(97 + i % 26)


async def test_when_a_big_agent_contains_the_tools_then_the_identical_small_one_ranks_first(
    client, register
):
    pid = _project(client)
    small = ["read_ticket", "close_ticket", "merge_ticket"]
    big = [*small, *(f"extra{_letters(i)}" for i in range(57))]
    await register(pid, "target", small)
    await register(pid, "twin", small)
    await register(pid, "big", big)

    matches = _similar(client, pid, "target").json()["matches"]

    assert [m["agent"] for m in matches] == ["twin", "big"]
    assert matches[0]["score"] == pytest.approx(1.0)  # no roles: the other two
    assert matches[1]["signals"]["tools"] < 0.1


async def test_when_one_extra_tool_is_added_then_the_score_barely_moves(
    client, register
):
    pid = _project(client)
    shared = [f"t{chr(97 + i) * 4}" for i in range(10)]
    await register(pid, "a", shared)
    await register(pid, "b", [*shared, "essay"])

    (match,) = _similar(client, pid, "a").json()["matches"]

    # Ten shared tools against the union of eleven, the extra one rarer.
    rare = similar.rarity
    assert match["signals"]["tools"] == pytest.approx(
        10 * rare(2, 2) / (10 * rare(2, 2) + rare(1, 2))
    )
    assert match["score"] == pytest.approx(0.9, abs=0.05)


async def test_when_every_agent_has_a_word_then_sharing_it_adds_almost_nothing(
    client, register
):
    pid = _project(client)
    for letter in "abcdefgh":
        await register(pid, letter, [f"inventory_{letter * 5}"])

    (match, *_) = _similar(client, pid, "a").json()["matches"]

    assert match["reasons"]["shared_name_words"] == ["inventory"]
    assert match["score"] < 0.05


async def test_when_a_word_differs_only_by_its_plural_then_it_matches(client, register):
    pid = _project(client)
    await register(pid, "a", ["export_invoices"])
    await register(pid, "b", ["send_invoice"])
    await register(pid, "c", ["list_branches"])
    await register(pid, "d", ["delete_branch"])
    await register(pid, "e", ["query_database"])
    await register(pid, "f", ["list_databases"])

    by_name = {m["agent"]: m for m in _similar(client, pid, "a").json()["matches"]}
    boxes = {m["agent"]: m for m in _similar(client, pid, "c").json()["matches"]}

    assert by_name["b"]["reasons"]["shared_name_words"] == ["invoice"]
    assert boxes["d"]["reasons"]["shared_name_words"] == ["branch"]
    # Only an "s" follows these, not an "es".
    databases = _similar(client, pid, "e").json()["matches"]
    assert databases[0]["reasons"]["shared_name_words"] == ["database"]


async def test_when_agents_are_compared_then_every_score_lies_between_0_and_1(
    client, register
):
    pid = _project(client)
    assert _put_file(client, pid, "policy.yaml", ENTRY).status_code == 200
    await register(pid, "refund_bot", ["issue_refund"])
    await register(pid, "billing_bot", ["issue_refund", "export_invoices"])
    await register(pid, "clone_bot", ["issue_refund"])

    matches = _similar(client, pid, "refund_bot").json()["matches"]

    assert matches
    for m in matches:
        assert 0 < m["score"] <= 1
        assert all(0 <= v <= 1 for v in m["signals"].values())


async def test_when_target_has_no_policy_section_then_candidates_are_scored_alike(
    client, register
):
    pid = _project(client)
    assert _put_file(client, pid, "policy.yaml", ENTRY).status_code == 200
    for name in ("new_bot", "refund_bot", "other_bot"):
        await register(pid, name, ["issue_refund"])

    matches = _similar(client, pid, "new_bot").json()["matches"]

    # refund_bot has a policy section and other_bot has none: the one with a
    # policy to copy must not rank below the one without.
    scores = {m["agent"]: m["score"] for m in matches}
    assert scores == {"other_bot": pytest.approx(1.0), "refund_bot": pytest.approx(1.0)}


def test_when_weights_are_summed_then_they_equal_1():
    assert sum(similar.SIGNAL_WEIGHTS.values()) == pytest.approx(1.0)
