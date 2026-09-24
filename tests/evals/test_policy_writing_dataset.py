"""The policy-writing eval set stays solvable as the policy language evolves.

Runs evals/policy_writing/run.py with the hand-written reference solutions
(no LLM) and with a do-nothing agent. Every reference must pass, and the
untouched fixtures must fail every case, so a scorer that passes everything
is caught too.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RUN = Path(__file__).resolve().parents[2] / "evals" / "policy_writing" / "run.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUN), *args], capture_output=True, text=True, timeout=600
    )


def test_reference_solutions_pass_every_case() -> None:
    proc = _run("--agent", "reference")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FAIL" not in proc.stdout


def test_untouched_fixtures_fail_every_case() -> None:
    proc = _run("--agent-cmd", "true")
    assert proc.returncode == 1
    assert "PASS " not in proc.stdout, proc.stdout
