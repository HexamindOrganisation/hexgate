"""Every shared starting project loads, lints clean and keeps its `preserve.yaml`.

A case's own tests cover the project it starts from; this covers each project
alone, through one synthetic case that asks for nothing, so a project no case
uses yet is checked too.
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
    agent = views[0]["name"]
    root = tmp_path / "root"
    shutil.copytree(PROJECTS / project, root / "starting_projects" / project)
    case_dir = root / "cases" / "x" / project
    case_dir.mkdir(parents=True)
    case = {"starting_project": project, "agent": agent, "request": "r", "expect": {}}
    (case_dir / "case.yaml").write_text(yaml.safe_dump(case))
    ws = tmp_path / "ws"
    ws.mkdir()

    [loaded] = load_cases(root)

    assert loaded["expect"].get("decisions"), f"{project}: no preserve.yaml calls"
    assert not failed(run_answer(loaded, ws, None))
