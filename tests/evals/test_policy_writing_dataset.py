"""The policy-writing eval set stays solvable, and its scorer stays discriminating.

No LLM, no Docker: our answers stand in for the agent.
- every `solution/` must pass its case;
- doing nothing must fail every case that asks for a change;
- every `wrong_answer/` must fail its case.
The loader's own rules are checked on small synthetic eval sets in `tmp_path`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from evals.policy_writing.cases import (
    CaseError,
    apply_solution,
    load_cases,
    starting_files,
)
from evals.policy_writing.checks import score, snapshot

CASES = load_cases()
# Doing nothing is the right answer to "tidy up without changing behaviour".
NOOP_PASSES = {"robustness/refactor_no_behaviour_change"}


def _run(case: dict, ws: Path, folder: str | None) -> list:
    """Score one of our answers (or none) as if an agent had made those edits."""
    for rel, text in starting_files(case).items():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(text)
    before = snapshot(ws)
    answer = apply_solution(case, ws, folder) if folder else ""
    return score(case, ws, before, answer)


def _failed(checks) -> list[str]:
    return [f"{c.name}: {c.detail}" for c in checks if not c.passed]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_solution_passes(case: dict, tmp_path: Path) -> None:
    assert not _failed(_run(case, tmp_path, "solution"))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_doing_nothing_passes_only_where_nothing_is_asked(
    case: dict, tmp_path: Path
) -> None:
    passed = not _failed(_run(case, tmp_path, None))
    assert passed == (case["id"] in NOOP_PASSES)


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if (c["dir"] / "wrong_answer").is_dir()],
    ids=lambda c: c["id"],
)
def test_wrong_answer_fails(case: dict, tmp_path: Path) -> None:
    assert _failed(_run(case, tmp_path, "wrong_answer"))


# ---------------------------------------------------------------- the loader

TOOLS_MD = """\
## Tools

| Tool | Arguments |
|---|---|
| `refund_order` | `order_id: string`, `amount: number` |
"""

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
    _write(shop / "TOOLS.md", TOOLS_MD)
    _write(shop / "policy.yaml", POLICY)
    if boundary:
        _write(shop / "policies" / "boundaries" / "org.yaml", "boundary: {}\n")
    if preserve is not None:
        _write(shop / "preserve.yaml", preserve)
    _write(root / "cases" / "cat" / "c" / "case.yaml", case)
    return root


def _case(**extra) -> dict:
    return {"starting_project": "shop", "request": "Do it.", "expect": {}, **extra}


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
        # A misspelt tool or argument is denied whatever the policy says.
        (
            _case(expect={"decisions": [{**DENY_500, "tool": "refund_ordr"}]}),
            r"not in TOOLS.md: \['refund_ordr'\]",
        ),
        (
            _case(expect={"decisions": [{**DENY_500, "args": {"amout": 501}}]}),
            r"not in TOOLS.md: \['args.amout'\]",
        ),
        (
            _case(expect={"decisions": [{**DENY_500, "attributes": {"tier": "x"}}]}),
            r"not in TOOLS.md: \['ctx.tier'\]",
        ),
        (
            _case(
                expect={
                    "superset": [
                        {"wider": "a", "narrower": "b", "probes": [{"tool": "refnd"}]}
                    ]
                }
            ),
            r"not in TOOLS.md: \['refnd'\]",
        ),
        (
            _case(expect={"decisions": [{**DENY_500, "tool": "agent.toool:support"}]}),
            r"not in TOOLS.md: \['agent.toool:support'\]",
        ),
        (_case(starting_project="missing"), "no starting project"),
        ({"request": "Do it.", "expect": {}}, "neither"),
    ],
)
def test_load_rejects(tmp_path: Path, case: dict, error: str) -> None:
    root = _eval_set(tmp_path, case)
    with pytest.raises(CaseError, match=error):
        load_cases(root)


def test_load_rejects_both_starting_projects(tmp_path: Path) -> None:
    root = _eval_set(tmp_path, _case())
    _write(root / "cases" / "cat" / "c" / "starting_project" / "TOOLS.md", TOOLS_MD)
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


def test_load_accepts_synthetic_tools_and_their_arguments(tmp_path: Path) -> None:
    calls = [
        {"tool": "net.http_request", "args": {"host": "x.com", "method": "GET"}},
        {"tool": "agent.tool:billing", "args": {"target": "billing"}},
    ]
    decisions = [{**c, "role": "billing", "expect": "deny"} for c in calls]
    load_cases(_eval_set(tmp_path, _case(expect={"decisions": decisions})))


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
    root = _eval_set(tmp_path, {"request": "Do it.", "held_out": True, "expect": {}})
    own = root / "cases" / "cat" / "c" / "starting_project"
    _write(own / "TOOLS.md", TOOLS_MD)
    [case] = load_cases(root)
    assert (case["id"], case["category"], case["held_out"]) == ("cat/c", "cat", True)
    assert case["project"] == own


@pytest.mark.parametrize(
    ("changed", "unchanged"),
    [
        ([], ["TOOLS.md", "policies/boundaries/org.yaml"]),
        # A case that means to edit a boundary lists it, and it isn't protected.
        (["policies/boundaries/org.yaml"], ["TOOLS.md"]),
    ],
)
def test_protected_files_are_unchanged_by_default(
    tmp_path: Path, changed: list, unchanged: list
) -> None:
    [case] = load_cases(_eval_set(tmp_path, _case(expect={"changed": changed})))
    assert sorted(case["expect"]["unchanged"]) == unchanged


def test_an_answer_editing_tools_md_fails_though_the_case_never_names_it(
    tmp_path: Path,
) -> None:
    case = _case(expect={"decisions": [REFUND_500]})
    root = _eval_set(tmp_path, case, boundary=False)
    _write(root / "cases" / "cat" / "c" / "wrong_answer" / "TOOLS.md", TOOLS_MD + "\n")
    [case] = load_cases(root)
    assert _failed(_run(case, tmp_path / "ws", "wrong_answer")) == [
        "unchanged: TOOLS.md: "
    ]


def test_preserve_yaml_and_dot_paths_are_not_given_to_the_agent(
    tmp_path: Path,
) -> None:
    root = _eval_set(tmp_path, _case(), preserve=[REFUND_500])
    _write(root / "starting_projects" / "shop" / ".claude" / "TOOLS.md", TOOLS_MD)
    [case] = load_cases(root)
    assert "preserve.yaml" not in starting_files(case)
    assert ".claude/TOOLS.md" not in starting_files(case)


def test_a_preserved_call_is_checked_in_every_case_of_its_project(
    tmp_path: Path,
) -> None:
    root = _eval_set(tmp_path, _case(), preserve=[REFUND_500])
    _write(root / "cases" / "cat" / "d" / "case.yaml", _case())
    assert [c["expect"]["decisions"] for c in load_cases(root)] == [[REFUND_500]] * 2


BOTH_ROLES = {
    "roles": ["billing", "support"],
    "tool": "refund_order",
    "expect": "allow",
}
BOTH_ROLES["args"] = REFUND_500["args"]


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
