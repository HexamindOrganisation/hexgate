"""The loader reads each case directory into a case the scorer can trust.

Each rule is checked on a small synthetic eval set in `tmp_path`: one project
`shop`, and case `cat/c` on it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from evals.policy_writing.cases import (
    CaseError,
    apply_solution,
    load_cases,
    starting_files,
)
from tests.evals.answers import failed, run_answer
from tests.evals.helpers import AGENT, agent_view, manifest_tool

AGENTS_JSON = json.dumps(
    [
        agent_view(
            AGENT, manifest_tool("refund_order", order_id="string", amount="number")
        ),
        # Another agent in the project: its tools and attributes are not shop-bot's.
        agent_view("ops-bot", manifest_tool("wire_transfer", iban="string")),
    ]
)
# `audit_decisions` rows (AuditDecisionRow fields); only their attributes matter here.
AUDIT_JSON = json.dumps(
    [
        {"agent_name": AGENT, "tool_name": "refund_order", "attributes": {"tier": "x"}},
        {
            "agent_name": "ops-bot",
            "tool_name": "wire_transfer",
            "attributes": {"eu": 1},
        },
    ]
)

POLICY = """\
version: 1
roles:
  default:
    tools: {}
  billing:
    tools:
      refund_order:
        mode: allow
        constraints:
          - args.amount <= 500
"""

REFUND_500 = {
    "role": "billing",
    "tool": "refund_order",
    "args": {"order_id": "o1", "amount": 500},
    "expect": "allow",
}
REFUND_10 = {**REFUND_500, "args": {"order_id": "o1", "amount": 10}}
DENY_500 = {**REFUND_500, "expect": "deny"}


def _write(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else yaml.safe_dump(content))


def _eval_set(
    tmp_path: Path, case: dict, *, preserve: list | None = None, boundary=True
) -> Path:
    """A one-project eval set: project `shop`, and case `cat/c` on it.

    With `boundary`, `shop` has a boundary module, which is enough for the
    loader but turns the scorer to module mode, where it isn't valid.
    """
    root = tmp_path / "set"
    shop = root / "starting_projects" / "shop"
    _write(shop / "agents.json", AGENTS_JSON)
    _write(shop / "audit.json", AUDIT_JSON)
    _write(shop / "policy.yaml", POLICY)
    if boundary:
        _write(shop / "policies" / "boundaries" / "org.yaml", "boundary: {}\n")
    if preserve is not None:
        _write(shop / "preserve.yaml", preserve)
    _write(root / "cases" / "cat" / "c" / "case.yaml", case)
    return root


def _case(**extra) -> dict:
    return {
        "starting_project": "shop",
        "agent": AGENT,
        "request": "Do it.",
        "expect": {},
        **extra,
    }


@pytest.mark.parametrize(
    ("case", "error"),
    [
        (_case(expect={"decision": []}), "expect.decision\n  Extra inputs"),
        (_case(id="x"), "come from the path"),
        (_case(category="x"), "come from the path"),
        (_case(expects={}), "expects\n  Extra inputs"),
        (_case(held_out="yes"), "held_out\n  Input should be a valid boolean"),
        (_case(expect={"mentions_any": "refund"}), "mentions_any\n  Input should be"),
        # A misspelt key would dry-run the call without its arguments.
        (_case(expect={"decisions": [{**REFUND_500, "arg": {}}]}), "arg\n  Extra"),
        (_case(expect={"decisions": [{**REFUND_500, "roles": ["a"]}]}), "exactly one"),
        (_case(expect={"decisions": [{**REFUND_500, "expect": []}]}), "at least 1"),
        # A blank `args:` is null; the scorer reads every one of these as a dict.
        (_case(expect={"decisions": [{**REFUND_500, "args": None}]}), "args\n  Input"),
        (
            _case(expect={"decisions": [{**REFUND_500, "attributes": None}]}),
            "attributes\n  Input",
        ),
        (
            _case(expect={"decisions": [{**REFUND_500, "run_facts": None}]}),
            "run_facts\n  Input",
        ),
        (_case(expect={"mentions_all": [1]}), "mentions_all.0\n  Input"),
        (
            _case(expect={"mentions_any": []}),
            "mentions_any\n  List should have at least 1",
        ),
        (
            _case(expect={"superset": [{"wider": "a", "narrower": "b", "probes": []}]}),
            "probes\n  List should have at least 1",
        ),
        (
            _case(expect={"decisions": [{**REFUND_500, "expect": "allowed"}]}),
            "should be 'allow'",
        ),
        (
            _case(
                expect={"superset": [{"wider": "a", "narrower": "b", "probes": [{}]}]}
            ),
            "probes.0.tool\n  Field required",
        ),
        # A misspelt path is missing before and after, so it would always pass.
        (_case(expect={"unchanged": ["TOOL.md"]}), "names no starting file"),
        # One of each call rule, raised against the case file (test_calls.py has
        # the rules themselves).
        (
            _case(expect={"decisions": [{**DENY_500, "tool": "refund_ordr"}]}),
            r"case.yaml: refund_ordr: unknown to shop-bot: \['refund_ordr'\]",
        ),
        (
            _case(
                expect={
                    "superset": [
                        {"wider": "a", "narrower": "b", "probes": [{"tool": "refnd"}]}
                    ]
                }
            ),
            r"unknown to shop-bot: \['refnd'\]",
        ),
        (
            _case(
                expect={
                    "decisions": [
                        {**DENY_500, "args": {"order_id": "o1", "amount": "51"}}
                    ]
                }
            ),
            "case.yaml: refund_order: not shop-bot's schema: .*amount='51'",
        ),
        (
            _case(
                expect={
                    "decisions": [
                        {**DENY_500, "tool": "agent.run", "args": {"agent": "x"}}
                    ]
                }
            ),
            r"case.yaml: agent.run: not what shop-bot sends: .*drop \['agent'\]",
        ),
        # Run facts the scorer would refuse (test_policy.py has the rules).
        (
            _case(
                expect={
                    "decisions": [
                        {**DENY_500, "tool": "agent.run", "args": {}}
                        | {"run_facts": {"tool_calls": 1}}
                    ]
                }
            ),
            "case.yaml: agent.run: agent.run is decided outside any run",
        ),
        (
            _case(
                expect={
                    "decisions": [
                        {**DENY_500, "tool": "net.tcp_connect"}
                        | {"args": {"host": "x.com", "port": 25}}
                        | {"run_facts": {"tool_calls": 1}}
                    ]
                }
            ),
            "case.yaml: net.tcp_connect: net.tcp_connect is decided outside any run",
        ),
        (
            _case(
                expect={"decisions": [{**DENY_500, "run_facts": {"agent": "ops-bot"}}]}
            ),
            "case.yaml: refund_order: run.agent is the case's agent",
        ),
        (
            _case(
                expect={
                    "decisions": [
                        {**DENY_500, "tool": "agent.handoff:ops-bot", "args": {}}
                        | {"run_facts": {"calls_of_this_tool": 1}}
                    ]
                }
            ),
            "case.yaml: agent.handoff:ops-bot: .* is never counted",
        ),
        (
            _case(expect={"decisions": [{**DENY_500, "run_facts": {"nosuch": 1}}]}),
            r"case.yaml: refund_order: unknown run.\* path\(s\) \['nosuch'\]",
        ),
        (_case(starting_project="missing"), "no starting project"),
        ({"agent": AGENT, "request": "Do it.", "expect": {}}, "neither"),
        (_case(agent=""), "agent\n  String should have at least 1"),
        (
            {"starting_project": "shop", "request": "Do it.", "expect": {}},
            "agent\n  Field",
        ),
        # The scorer reads this agent's manifest and audit rows.
        (_case(agent="shop-bott"), "no manifest for agent 'shop-bott'"),
    ],
)
def test_load_rejects(tmp_path: Path, case: dict, error: str) -> None:
    root = _eval_set(tmp_path, case)
    with pytest.raises(CaseError, match=error):
        load_cases(root)


def test_load_rejects_both_starting_projects(tmp_path: Path) -> None:
    root = _eval_set(tmp_path, _case())
    _write(
        root / "cases" / "cat" / "c" / "starting_project" / "agents.json", AGENTS_JSON
    )
    with pytest.raises(CaseError, match="both"):
        load_cases(root)


def test_load_rejects_a_case_folder_without_case_yaml(tmp_path: Path) -> None:
    root = _eval_set(tmp_path, _case())
    _write(root / "cases" / "cat" / "d" / "case.yml", _case())
    with pytest.raises(CaseError, match="no case.yaml"):
        load_cases(root)


def test_load_rejects_a_case_one_level_too_shallow(tmp_path: Path) -> None:
    root = _eval_set(tmp_path, _case())
    _write(root / "cases" / "d" / "case.yaml", _case())
    with pytest.raises(CaseError, match="not at cases/<category>/<name>"):
        load_cases(root)


def test_load_rejects_an_unquoted_yaml_date(tmp_path: Path) -> None:
    # YAML makes a date of it, and the strict case model keeps it as one.
    root = _eval_set(tmp_path, _case())
    call = "{role: billing, tool: refund_order, args: {order_id: 2026-03-01, amount: 1}, expect: deny}"
    _write(
        root / "cases" / "cat" / "c" / "case.yaml",
        f"starting_project: shop\nagent: {AGENT}\nrequest: Do it.\n"
        f"expect:\n  decisions:\n    - {call}\n",
    )
    with pytest.raises(CaseError, match="order_id=datetime.date.* is not JSON"):
        load_cases(root)


def test_load_rejects_an_unquoted_yaml_date_beside_a_preserve_file(
    tmp_path: Path,
) -> None:
    # Merging with preserve.yaml keys each call by its JSON; a date must not crash it.
    root = _eval_set(tmp_path, _case(), preserve=[REFUND_10])
    call = "{role: billing, tool: refund_order, args: {order_id: o1, amount: 1}, attributes: {tier: 2026-03-01}, expect: deny}"
    _write(
        root / "cases" / "cat" / "c" / "case.yaml",
        f"starting_project: shop\nagent: {AGENT}\nrequest: Do it.\n"
        f"expect:\n  decisions:\n    - {call}\n",
    )
    with pytest.raises(CaseError, match="ctx.tier=datetime.date.* is not string"):
        load_cases(root)


def test_load_completes_and_checks_a_script_call(tmp_path: Path) -> None:
    # complete requires a script's invocation arguments; unknown_names must
    # then accept them.
    view = agent_view(
        AGENT, manifest_tool("refund_order", order_id="string", amount="number")
    )
    view["manifest"]["skills"] = [{"name": "pdf", "description": "pdf"}]
    root = _eval_set(tmp_path, _case())
    _write(root / "starting_projects" / "shop" / "agents.json", json.dumps([view]))
    run = {"file_path": "s.sh", "content_hash": None, "script_args": None}
    run |= {"short_options": None, "positional_args": None}
    call = {
        "role": "billing",
        "tool": "skill.script:pdf",
        "args": run,
        "expect": "deny",
    }
    _write(
        root / "cases" / "cat" / "c" / "case.yaml", _case(expect={"decisions": [call]})
    )
    [case] = load_cases(root)
    assert case["expect"]["decisions"][0]["args"]["via"] == "script"


def test_a_loaded_reach_call_is_one_decide_accepts(tmp_path: Path) -> None:
    # decide fills a gate's args itself; the loader leaves them out.
    reach = {"role": "billing", "tool": "agent.tool:ops-bot", "expect": "allow"}
    root = _eval_set(tmp_path, _case(expect={"decisions": [reach]}), boundary=False)
    # A declared reach gate, where decide refuses args other than the gate's.
    declared = POLICY + "    agents:\n      ops-bot:\n        mode: allow\n"
    _write(root / "starting_projects" / "shop" / "policy.yaml", declared)
    [case] = load_cases(root)
    [decision] = [
        c for c in run_answer(case, tmp_path / "ws", None) if "ops-bot" in c.name
    ]
    assert decision.passed, decision.detail


def test_a_case_overrides_a_preserved_reach(tmp_path: Path) -> None:
    reach = {"role": "billing", "tool": "agent.tool:ops-bot"}
    case = _case(expect={"decisions": [{**reach, "expect": "deny"}]})
    root = _eval_set(tmp_path, case, preserve=[{**reach, "expect": "allow"}])
    [case] = load_cases(root)
    assert [d["expect"] for d in case["expect"]["decisions"]] == ["deny"]


def test_load_rejects_a_misnamed_case_folder(tmp_path: Path) -> None:
    root = _eval_set(tmp_path, _case())
    (root / "cases" / "cat" / "c" / "wrong_answers").mkdir()
    with pytest.raises(CaseError, match=r"not part of a case: \['wrong_answers'\]"):
        load_cases(root)


@pytest.mark.parametrize("name", ["preserve.yml", "Preserve.yaml"])
def test_load_rejects_a_misnamed_preserve_file(tmp_path: Path, name: str) -> None:
    root = _eval_set(tmp_path, _case())
    _write(root / "starting_projects" / "shop" / name, [REFUND_500])
    with pytest.raises(CaseError, match=rf"\['{name}'\] should be preserve"):
        load_cases(root)


def test_load_skips_dot_folders(tmp_path: Path) -> None:
    root = _eval_set(tmp_path, _case())
    (root / "cases" / "cat" / ".ipynb_checkpoints").mkdir()
    assert [c["id"] for c in load_cases(root)] == ["cat/c"]


def test_load_rejects_a_bad_preserved_decision(tmp_path: Path) -> None:
    root = _eval_set(tmp_path, _case(), preserve=REFUND_500)
    with pytest.raises(CaseError, match="(?s)preserve.yaml: .*valid list"):
        load_cases(root)


def test_load_takes_id_and_category_from_the_path(tmp_path: Path) -> None:
    root = _eval_set(
        tmp_path, {"agent": AGENT, "request": "Do it.", "held_out": True, "expect": {}}
    )
    own = root / "cases" / "cat" / "c" / "starting_project"
    _write(own / "agents.json", AGENTS_JSON)
    [case] = load_cases(root)
    assert (case["id"], case["category"], case["held_out"]) == ("cat/c", "cat", True)
    assert case["project"] == own


@pytest.mark.parametrize(
    ("changed", "unchanged"),
    [
        ([], ["agents.json", "audit.json", "policies/boundaries/org.yaml"]),
        # A case that means to edit a boundary lists it, and it isn't protected.
        (["policies/boundaries/org.yaml"], ["agents.json", "audit.json"]),
    ],
)
def test_protected_files_are_unchanged_by_default(
    tmp_path: Path, changed: list, unchanged: list
) -> None:
    [case] = load_cases(_eval_set(tmp_path, _case(expect={"changed": changed})))
    assert sorted(case["expect"]["unchanged"]) == unchanged


def test_only_protected_files_the_project_has_are_unchanged(tmp_path: Path) -> None:
    # audit.json is optional, and the scorer fails an `unchanged` file neither
    # snapshot has.
    root = _eval_set(tmp_path, _case())
    (root / "starting_projects" / "shop" / "audit.json").unlink()
    [case] = load_cases(root)
    assert "audit.json" not in case["expect"]["unchanged"]


def test_an_answer_editing_agents_json_fails_though_the_case_never_names_it(
    tmp_path: Path,
) -> None:
    case = _case(expect={"decisions": [REFUND_500]})
    root = _eval_set(tmp_path, case, boundary=False)
    _write(
        root / "cases" / "cat" / "c" / "wrong_answer" / "agents.json",
        AGENTS_JSON + "\n",
    )
    [case] = load_cases(root)
    # The scorer also stops trusting the names an edited agents.json lists.
    assert "unchanged: agents.json: " in failed(
        run_answer(case, tmp_path / "ws", "wrong_answer")
    )


def test_preserve_yaml_and_dot_paths_are_not_given_to_the_agent(
    tmp_path: Path,
) -> None:
    root = _eval_set(tmp_path, _case(), preserve=[REFUND_500])
    _write(root / "starting_projects" / "shop" / ".claude" / "agents.json", AGENTS_JSON)
    [case] = load_cases(root)
    assert "preserve.yaml" not in starting_files(case)
    assert ".claude/agents.json" not in starting_files(case)


def test_a_preserved_call_is_checked_in_every_case_of_its_project(
    tmp_path: Path,
) -> None:
    root = _eval_set(tmp_path, _case(), preserve=[REFUND_500])
    _write(root / "cases" / "cat" / "d" / "case.yaml", _case())
    assert [c["expect"]["decisions"] for c in load_cases(root)] == [[REFUND_500]] * 2


BOTH_ROLES = {
    "roles": ["billing", "support"],
    "tool": "refund_order",
    "args": REFUND_500["args"],
    "expect": "allow",
}


@pytest.mark.parametrize(
    ("own", "preserved", "merged"),
    [
        # The same call: the case's entry replaces the preserved one.
        ([DENY_500], [REFUND_500], [DENY_500]),
        # Another call on the same tool: both are checked.
        ([REFUND_10], [REFUND_500], [REFUND_10, REFUND_500]),
        # The same call for one role of a `roles` entry: only that role goes.
        ([DENY_500], [BOTH_ROLES], [DENY_500, {**BOTH_ROLES, "roles": ["support"]}]),
    ],
)
def test_merge_preserved_matches_on_the_whole_call(
    tmp_path: Path, own: list, preserved: list, merged: list
) -> None:
    case = _case(expect={"decisions": own})
    [case] = load_cases(_eval_set(tmp_path, case, preserve=preserved))
    assert case["expect"]["decisions"] == merged


def test_answer_md_is_the_answer_not_a_project_file(tmp_path: Path) -> None:
    root = _eval_set(tmp_path, _case(), boundary=False)
    solution = root / "cases" / "cat" / "c" / "solution"
    _write(solution / "ANSWER.md", "Capped at 500.")
    _write(solution / "policy.yaml", POLICY)
    [case] = load_cases(root)
    ws = tmp_path / "ws"
    ws.mkdir()
    assert apply_solution(case, ws) == "Capped at 500."
    assert [p.name for p in ws.iterdir()] == ["policy.yaml"]
