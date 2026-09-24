"""The policy-writing eval set stays solvable, and its scorer stays discriminating.

Runs evals/policy_writing/run.py without any LLM:
- the hand-written reference solutions must pass every case;
- a do-nothing agent must fail every case that asks for a change;
- known-bad answers (negatives/) must fail the case they target.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parents[2] / "evals" / "policy_writing"
RUN = EVALS / "run.py"

# Doing nothing is the right answer to "tidy up without changing behaviour".
NO_OP_PASSES = {"e03_refactor_no_behaviour_change"}


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUN), *args], capture_output=True, text=True, timeout=600
    )


def _passed(stdout: str) -> set[str]:
    return set(re.findall(r"^PASS\s+(\S+)#", stdout, flags=re.M))


def test_reference_solutions_pass_every_case() -> None:
    proc = _run("--agent", "reference")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FAIL" not in proc.stdout


def test_doing_nothing_fails_every_case_that_asks_for_a_change() -> None:
    proc = _run("--agent-cmd", "true")
    assert _passed(proc.stdout) == NO_OP_PASSES, proc.stdout


def test_known_bad_answers_fail() -> None:
    cases = sorted(p.name for p in (EVALS / "negatives").iterdir() if p.is_dir())
    args = [a for c in cases for a in ("--case", c)]
    proc = _run("--agent", "negatives", *args)
    assert len(re.findall(r"^FAIL ", proc.stdout, flags=re.M)) == len(cases), (
        proc.stdout
    )
    assert not _passed(proc.stdout), proc.stdout
