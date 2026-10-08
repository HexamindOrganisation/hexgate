"""The MCP stand-ins read (`sources.py`): agents.json and audit.json."""

from __future__ import annotations

import json

import pytest

from evals.policy_writing.sources import (
    SourceError,
    load_attributes,
    load_project_agents,
)
from tests.evals.helpers import (
    AGENT,
    ATTRS,
    AUDIT,
    agent_view,
    make_workspace,
    manifest_tool,
)

# load_attributes


def test_load_attributes_happy_path(tmp_path) -> None:
    # `region` is only ops-bot's.
    assert load_attributes(make_workspace(tmp_path), AGENT) == ATTRS


def test_when_audit_json_is_the_endpoints_page_then_its_rows_are_read(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    page = {"rows": AUDIT, "total": len(AUDIT), "limit": 25, "offset": 0}
    (ws / "audit.json").write_text(json.dumps(page))
    assert load_attributes(ws, AGENT) == ATTRS


def test_when_audit_json_is_missing_then_no_attribute_is_known(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    (ws / "audit.json").unlink()
    assert load_attributes(ws, AGENT) == set()


@pytest.mark.parametrize(
    "broken",
    ["", "{not json", "null", "[{}]"],  # [{}]: a row with no agent_name
)
def test_when_audit_json_is_unreadable_then_load_attributes_fails(
    tmp_path, broken
) -> None:
    ws = make_workspace(tmp_path)
    (ws / "audit.json").write_text(broken)
    with pytest.raises(SourceError):
        load_attributes(ws, AGENT)


# load_project_agents


def test_load_project_agents_happy_path(tmp_path) -> None:
    agents = load_project_agents(make_workspace(tmp_path))
    assert agents.registered == {"shop-bot", "ops-bot", "draft-bot"}
    assert agents.manifests.keys() == {"shop-bot", "ops-bot"}  # draft-bot has none
    assert {t.name for t in agents.manifests[AGENT].tools} == {
        "view_orders",
        "refund_order",
    }


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
    "broken",
    [
        "",
        "{not json",
        '{"agents": []}',  # not the endpoint's list
        "[{}]",  # a view with no name
        '[{"name": "a", "manifest": {}}]',  # a manifest with no tools
        None,  # the starting project ships none
    ],
)
def test_when_agents_json_is_unreadable_then_load_project_agents_fails(
    tmp_path, broken
) -> None:
    ws = make_workspace(tmp_path)
    if broken is None:
        (ws / "agents.json").unlink()
    else:
        (ws / "agents.json").write_text(broken)
    with pytest.raises(SourceError):
        load_project_agents(ws)
