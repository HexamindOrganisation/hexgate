"""The Inspect task scores our answers on the real case as the dataset tests do.

No model: `--model none`. The local sandbox stands in for PR 5's Docker one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("inspect_ai")

from inspect_ai import eval as inspect_eval  # noqa: E402
from inspect_ai.scorer import CORRECT, INCORRECT  # noqa: E402
from inspect_ai.util._sandbox.local import LocalSandboxEnvironment  # noqa: E402

from evals.policy_writing import task  # noqa: E402
from evals.policy_writing.cases import apply_solution, starting_files  # noqa: E402
from evals.policy_writing.checks import Check, snapshot  # noqa: E402

CASE = "explicit_request/support_refund_cap"
ANSWERS = ("solution", "wrong_answer", "preserve.yaml")
METRICS = {"accuracy", "stderr", "explicit_request", "held_out_False"}


def run(agent: str, log_dir: Path):
    """Run the task on the real case; its one sample's score."""
    (log,) = inspect_eval(
        task.policy_writing(agent=agent, cases=CASE),
        model="none",
        log_dir=str(log_dir),
        display="none",
    )
    assert log.status == "success", log.error
    (scored,) = log.results.scores
    assert set(scored.metrics) == METRICS
    (sample,) = log.samples
    return sample.scores["policy_checks"]


def test_reference_passes(tmp_path: Path) -> None:
    assert run("reference", tmp_path).value == CORRECT


def test_wrong_answer_fails(tmp_path: Path) -> None:
    score = run("wrong_answers", tmp_path)
    assert score.value == INCORRECT
    names = [c["name"] for c in score.metadata["checks"] if not c["passed"]]
    assert any(n.startswith("decision 1:") for n in names), names
    assert "only known arguments and attributes" in names


def test_noop_fails(tmp_path: Path) -> None:
    score = run("noop", tmp_path)
    assert score.value == INCORRECT
    assert "## Answer\n(none)\n" in score.explanation


def test_when_claude_then_task_build_names_pr_5() -> None:
    with pytest.raises(NotImplementedError, match="PR 5"):
        task.policy_writing(agent="claude")


@pytest.mark.parametrize("cases", ["a/", ["a/"], "zzz,a/", ["zzz", " a/ "]])
def test_cases_prefix_selects_the_matching_cases(cases, monkeypatch) -> None:
    # Synthetic ids, so the test still holds as categories gain cases.
    ids = ["a/one", "a/two", "b/one"]
    monkeypatch.setattr(task, "CASES", {i: {**task.CASES[CASE], "id": i} for i in ids})
    dataset = task.policy_writing(agent="reference", cases=cases).dataset
    assert [s.id for s in dataset] == ["a/one", "a/two"]


def test_only_wrong_answers_skips_a_case_without_it(tmp_path, monkeypatch) -> None:
    # Doing nothing is a valid solution, so `solution/` may be absent too.
    bare = {**task.CASES[CASE], "id": "x/bare", "dir": tmp_path}
    monkeypatch.setitem(task.CASES, "x/bare", bare)
    assert [s.id for s in task.samples("solution", ["x/"])] == ["x/bare"]
    assert [s.id for s in task.samples(None, ["x/"])] == ["x/bare"]
    assert task.samples("wrong_answer", ["x/"]) == []


def test_sample_holds_the_starting_project_and_nothing_of_ours() -> None:
    (sample,) = task.policy_writing(agent="reference", cases=CASE).dataset
    assert {"policy.yaml", "agents.json", "audit.json"} <= set(sample.files)
    assert not [f for f in sample.files if any(a in f for a in ANSWERS)]
    assert sample.input == "/write-policy Let support refund orders, up to 50 USD."
    assert json.loads(json.dumps(sample.metadata)) == {
        "case_id": CASE,
        "category": "explicit_request",
        "held_out": False,
    }


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode("utf-8"))


def test_hashes_match_snapshot(tmp_path: Path) -> None:
    # `grade` hashes `before` in memory; it must agree with `snapshot` of the same files.
    files = {"policy.yaml": "v: 1\n", "policies/org.yaml": "# café → 50 €\n"}
    write(tmp_path, files)
    assert task.hashes(files) == snapshot(tmp_path)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """The starting project with our solution on top, as the reference agent leaves it."""
    write(tmp_path, starting_files(task.CASES[CASE]))
    apply_solution(task.CASES[CASE], tmp_path)
    return tmp_path


def grade(ws: Path, answer: str = "", ran=None, size: tuple[int, int] = (3, 3)):
    return task.grade(task.CASES[CASE], ws, answer, size, ran)


def names(score) -> list[str]:
    return [c["name"] for c in score.metadata["checks"]]


def test_grade_happy_path(ws: Path) -> None:
    score = grade(ws, "Capped at 50 USD.")
    assert score.value == CORRECT
    assert "## Changes\n### policy.yaml\n```diff" in score.explanation


def test_when_agent_did_not_run_then_that_check_comes_first(ws: Path) -> None:
    score = grade(ws, ran=Check("agent ran", False, "time limit reached"))
    assert score.value == INCORRECT
    assert score.metadata["checks"][0] == {
        "name": "agent ran",
        "passed": False,
        "detail": "time limit reached",
    }


@pytest.mark.parametrize("size", [(task.MAX_FILES + 1, 10), (5, task.MAX_BYTES + 1)])
def test_when_past_a_bound_then_only_the_size_check(ws: Path, size) -> None:
    score = grade(ws, size=size)
    assert score.value == INCORRECT
    assert names(score) == ["workspace size"]
    assert f"{size[0]} files, {size[1]} bytes" in score.metadata["checks"][0]["detail"]
    assert "(not diffed: workspace past the bounds)" in score.explanation


def test_when_at_the_bounds_then_scored(ws: Path) -> None:
    score = grade(ws, size=(task.MAX_FILES, task.MAX_BYTES))
    assert "workspace size" not in names(score)


def test_diff_is_cut_at_max_lines(ws: Path) -> None:
    (ws / "policy.yaml").write_text("\n".join(f"line {i}" for i in range(250)))
    diff = grade(ws).explanation.split("```diff\n")[1]
    body = diff.split("\n```")[0].splitlines()
    assert len(body) == task.MAX_DIFF_LINES + 1
    assert body[-1].startswith("… ") and body[-1].endswith(" more lines")


def test_answer_is_cut_in_the_explanation_only(ws: Path) -> None:
    answer = "a" * 5_000
    score = grade(ws, answer)
    assert score.answer == answer
    assert "a" * 4_000 + "…" in score.explanation
    assert "a" * 4_001 not in score.explanation


# The sandbox side, on Inspect's own local sandbox (a temp folder).


def tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_push_answer_writes_what_apply_solution_does(tmp_path: Path) -> None:
    case = {"dir": tmp_path / "case"}
    write(
        case["dir"] / "solution",
        {"a.yaml": "1", "b c/d.yaml": "é", "ANSWER.md": "Done."},
    )
    sb, host = LocalSandboxEnvironment(), tmp_path / "host"
    answer = asyncio.run(task.push_answer(sb, case, "solution"))
    assert answer == apply_solution(case, host, "solution") == "Done."
    assert tree(Path(sb.directory.name)) == tree(host)


def test_pull_workspace_copies_within_bounds(tmp_path, monkeypatch) -> None:
    sb = LocalSandboxEnvironment()
    write(Path(sb.directory.name), {"a": "abc", "b c/d": "hello", ".claude/x": "no"})
    dest, over = tmp_path / "dest", tmp_path / "over"
    assert asyncio.run(task.pull_workspace(sb, dest)) == (2, 8)
    assert set(tree(dest)) == {"a", "b c/d"}
    monkeypatch.setattr(task, "MAX_FILES", 1)
    assert asyncio.run(task.pull_workspace(sb, over)) == (2, 8)
    assert not over.exists()
