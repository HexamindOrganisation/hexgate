"""The policy-writing eval set stays solvable, and its scorer stays discriminating.

No LLM, no Docker: our answers stand in for the agent.
- every `solution/` must pass its case;
- doing nothing must fail every case that asks for a change;
- every `wrong_answer/` must fail its case.
The loader's rules are in test_cases.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.policy_writing.cases import load_cases
from tests.evals.answers import failed, run_answer

CASES = load_cases()
# Doing nothing is the right answer to "tidy up without changing behaviour".
NOOP_PASSES = {"robustness/refactor_no_behaviour_change"}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_solution_passes(case: dict, tmp_path: Path) -> None:
    assert not failed(run_answer(case, tmp_path, "solution"))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_doing_nothing_passes_only_where_nothing_is_asked(
    case: dict, tmp_path: Path
) -> None:
    passed = not failed(run_answer(case, tmp_path, None))
    assert passed == (case["id"] in NOOP_PASSES)


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if (c["dir"] / "wrong_answer").is_dir()],
    ids=lambda c: c["id"],
)
def test_wrong_answer_fails(case: dict, tmp_path: Path) -> None:
    assert failed(run_answer(case, tmp_path, "wrong_answer"))
