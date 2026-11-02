"""Every starting project loads, lints clean and keeps its `preserve.yaml`.

A case's own tests cover the project it starts from, but its do-nothing run is
expected to fail, which hides a lint the untouched project already has. This
covers each project alone, shared or a case's own, through one synthetic case
that asks for nothing, so a project no case uses yet is checked too.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from evals.policy_writing.cases import HERE, load_cases
from tests.evals.answers import failed, run_answer

PROJECTS = HERE / "starting_projects"
# Cases that bring their own starting_project/, as `<category>/<name>`.
OWN = sorted(
    p.parent.relative_to(HERE / "cases").as_posix()
    for p in (HERE / "cases").glob("*/*/starting_project")
    if p.is_dir()
)


def _untouched(project: Path, agent: str, tmp_path: Path) -> tuple[dict, list[str]]:
    """Score `project` as left untouched, as `agent`'s: the loaded case and the
    checks that fail."""
    root = tmp_path / "root"
    shutil.copytree(project, root / "starting_projects" / "p")
    case_dir = root / "cases" / "x" / "p"
    case_dir.mkdir(parents=True)
    case = {"starting_project": "p", "agent": agent, "request": "r", "expect": {}}
    (case_dir / "case.yaml").write_text(yaml.safe_dump(case))
    ws = tmp_path / "ws"
    ws.mkdir()
    [loaded] = load_cases(root)
    return loaded, failed(run_answer(loaded, ws, None))


@pytest.mark.parametrize(
    "project",
    sorted(
        p.name for p in PROJECTS.iterdir() if p.is_dir() and not p.name.startswith(".")
    ),
)
def test_when_a_starting_project_is_untouched_then_its_preserved_calls_hold(
    project: str, tmp_path: Path
) -> None:
    # The project's one agent: the one its cases edit. A project with several
    # needs its own choice here, not the first one.
    views = json.loads((PROJECTS / project / "agents.json").read_text())
    assert len(views) == 1, f"{project}: {len(views)} agents in agents.json"
    loaded, failures = _untouched(PROJECTS / project, views[0]["name"], tmp_path)

    assert loaded["expect"].get("decisions"), f"{project}: no preserve.yaml calls"
    assert not failures


@pytest.mark.parametrize("case", OWN)
def test_when_a_case_own_starting_project_is_untouched_then_it_lints_clean(
    case: str, tmp_path: Path
) -> None:
    case_dir = HERE / "cases" / case
    agent = yaml.safe_load((case_dir / "case.yaml").read_text()).get("agent")

    _, failures = _untouched(case_dir / "starting_project", agent, tmp_path)

    assert not failures
