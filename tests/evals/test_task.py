"""The Inspect task scores our answers on the real case as the dataset tests do.

No model: `--model none`. The local sandbox stands in for PR 5's Docker one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("inspect_ai")

from inspect_ai import eval as inspect_eval  # noqa: E402
from inspect_ai.scorer import CORRECT, INCORRECT  # noqa: E402

from evals.policy_writing import task  # noqa: E402
from evals.policy_writing.cases import apply_solution, starting_files  # noqa: E402
from evals.policy_writing.checks import Check  # noqa: E402

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


def test_when_a_case_has_no_wrong_answer_then_only_wrong_answers_skips_it(
    tmp_path: Path, monkeypatch
) -> None:
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


# `grade` is the step after the pull: a workspace on the host, as the sandbox left it.


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """The starting project with our solution on top, as the reference agent leaves it."""
    task.write_files(tmp_path, starting_files(task.CASES[CASE]))
    apply_solution(task.CASES[CASE], tmp_path)
    return tmp_path


def grade(ws: Path, answer: str = "", ran: Check | None = None):
    return task.grade(task.CASES[CASE], ws, answer, ran)


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


@pytest.mark.parametrize(
    "files, size", [(task.MAX_FILES + 1, 1), (1, task.MAX_BYTES + 1)]
)
def test_when_past_a_bound_then_only_the_size_check(
    ws: Path, files: int, size: int
) -> None:
    for i in range(files):
        (ws / f"f{i}.txt").write_text("x" * size)
    score = grade(ws)
    assert score.value == INCORRECT
    assert names(score) == ["workspace size"]
    assert "(not diffed: workspace past the bounds)" in score.explanation


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
