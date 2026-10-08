"""The MCP stand-ins read (`sources.py`): agents.json and audit.json."""

from __future__ import annotations

import json

import pytest

from evals.policy_writing.sources import (
    SourceError,
    load_known_names,
    load_project_agents,
    load_project_names,
)
from tests.evals.helpers import (
    AGENT,
    AUDIT,
    KNOWN,
    agent_view,
    make_workspace,
    manifest_tool,
)

# load_known_names


def test_load_known_names_happy_path(tmp_path) -> None:
    # `region`, `wire_transfer` and `ledger` are only ops-bot's; draft-bot has no
    # manifest yet.
    assert load_known_names(make_workspace(tmp_path), AGENT) == KNOWN


def test_when_audit_json_is_the_endpoints_page_then_its_rows_are_read(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    page = {"rows": AUDIT, "total": len(AUDIT), "limit": 25, "offset": 0}
    (ws / "audit.json").write_text(json.dumps(page))
    assert load_known_names(ws, AGENT).attrs == {"department"}


def test_when_audit_json_is_missing_then_no_attribute_is_known(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    (ws / "audit.json").unlink()
    assert load_known_names(ws, AGENT).attrs == set()


def test_when_a_tool_has_no_description_then_its_names_still_load(tmp_path) -> None:
    # The platform's AgentManifestView allows a null description; the SDK's doesn't.
    tool = {**manifest_tool("view_orders", customer_id="string"), "description": None}
    ws = make_workspace(tmp_path)
    (ws / "agents.json").write_text(json.dumps([agent_view(AGENT, tool)]))
    assert load_known_names(ws, AGENT).tools == {"view_orders": {"customer_id"}}


def test_when_the_manifest_lists_no_skills_or_guards_then_none_are_known(
    tmp_path,
) -> None:
    ws = make_workspace(tmp_path)
    (ws / "agents.json").write_text(json.dumps([agent_view(AGENT)]))
    known = load_known_names(ws, AGENT)
    assert (known.skills, known.guards) == (set(), set())


@pytest.mark.parametrize("agent", ["billing-bot", "draft-bot"])  # absent, no manifest
def test_when_the_case_agent_has_no_manifest_then_load_known_names_fails(
    tmp_path, agent
) -> None:
    ws = make_workspace(tmp_path)
    with pytest.raises(SourceError, match=f"has no manifest for agent '{agent}'"):
        load_known_names(ws, agent)


@pytest.mark.parametrize(
    ("source", "broken"),
    [
        ("agents.json", ""),
        ("agents.json", "{not json"),
        ("agents.json", "[]"),
        ("agents.json", '{"agents": []}'),  # not the endpoint's list
        ("agents.json", "[{}]"),  # a view with no name
        ("audit.json", "null"),
        ("audit.json", "[{}]"),  # a row with no agent_name
        ("agents.json", None),  # the starting project ships none
    ],
)
def test_when_a_source_is_unreadable_then_load_known_names_fails(
    tmp_path, source, broken
) -> None:
    ws = make_workspace(tmp_path)
    if broken is None:
        (ws / source).unlink()
    else:
        (ws / source).write_text(broken)
    with pytest.raises(SourceError):
        load_known_names(ws, AGENT)


# load_project_names


def test_load_project_names_happy_path(tmp_path) -> None:
    # Every agent's tools, skills and guards; still shop-bot's attributes.
    names = load_project_names(make_workspace(tmp_path), AGENT)
    assert names.tools == {**KNOWN.tools, "wire_transfer": {"iban"}}
    assert (names.skills, names.guards) == ({"pdf", "ledger"}, KNOWN.guards)
    assert names.attrs == KNOWN.attrs


def test_when_two_agents_share_a_tool_then_load_project_names_merges_its_args(
    tmp_path,
) -> None:
    ws = make_workspace(tmp_path)
    views = [
        agent_view(AGENT, manifest_tool("lookup", a="string")),
        agent_view("ops-bot", manifest_tool("lookup", b="string")),
    ]
    (ws / "agents.json").write_text(json.dumps(views))
    assert load_project_names(ws, AGENT).tools == {"lookup": {"a", "b"}}


@pytest.mark.parametrize("agent", ["billing-bot", "draft-bot"])  # absent, no manifest
def test_when_the_case_agent_has_no_manifest_then_load_project_names_fails(
    tmp_path, agent
) -> None:
    with pytest.raises(SourceError, match=f"has no manifest for agent '{agent}'"):
        load_project_names(make_workspace(tmp_path), agent)


# load_project_agents


def test_load_project_agents_happy_path(tmp_path) -> None:
    agents = load_project_agents(make_workspace(tmp_path))
    assert agents.registered == {"shop-bot", "ops-bot", "draft-bot"}
    assert agents.manifests.keys() == {"shop-bot", "ops-bot"}  # draft-bot has none
    assert {t.name for t in agents.manifests[AGENT].tools} == KNOWN.tools.keys()


def test_when_a_tool_description_is_null_then_load_project_agents_reads_it(
    tmp_path,
) -> None:
    # The endpoint's view allows a null tool description; the SDK model doesn't.
    tool = {**manifest_tool("view_orders", customer_id="string"), "description": None}
    ws = make_workspace(tmp_path)
    (ws / "agents.json").write_text(json.dumps([agent_view(AGENT, tool)]))
    manifest = load_project_agents(ws).manifests[AGENT]
    assert manifest.tools[0].description == ""


@pytest.mark.parametrize(
    "broken", ["{not json", "[{}]", '[{"name": "a", "manifest": {}}]']
)
def test_when_agents_json_is_unreadable_then_load_project_agents_fails(
    tmp_path, broken
) -> None:
    ws = make_workspace(tmp_path)
    (ws / "agents.json").write_text(broken)
    with pytest.raises(SourceError):
        load_project_agents(ws)
