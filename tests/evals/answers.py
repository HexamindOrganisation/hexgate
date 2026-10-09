"""Score one of our answers to a loaded case, as the dataset and loader tests do."""

from __future__ import annotations

from pathlib import Path

from evals.policy_writing.cases import apply_solution, starting_files
from evals.policy_writing.checks import score, snapshot


def run_answer(case: dict, ws: Path, folder: str | None) -> list:
    """Score one of our answers (or none) as if an agent had made those edits."""
    for rel, text in starting_files(case).items():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(text)
    before = snapshot(ws)
    answer = apply_solution(case, ws, folder) if folder else ""
    return score(case, ws, before, answer)


def failed(checks) -> list[str]:
    return [f"{c.name}: {c.detail}" for c in checks if not c.passed]
