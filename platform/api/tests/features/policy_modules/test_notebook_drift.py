"""Notebook↔seed drift guard, shared across the showcase demos.

Each marimo demo under ``deploy/`` inlines the same compose policy the API seeds
first-boot, kept aligned by hand. Assert the two copies are byte-identical so an
edit to one can't silently drift from the other.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hexgate_api.features.policy_modules.devops_seed import DEVOPS_SEED_FILES
from hexgate_api.features.policy_modules.seed_data import SEED_POLICY_FILES


def _find_notebook(filename: str) -> Path | None:
    """Locate deploy/<filename> from the test file, or None."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "deploy" / filename
        if candidate.exists():
            return candidate
    return None


def _notebook_dict(src: str, name: str) -> dict:
    """Return the literal dict assigned to ``name`` in the notebook source.

    Parses the source with ``ast`` and walks for the assignment, rather than a
    regex over the text — so a cosmetic reformat (re-indent, a value that happens
    to contain ``\\n    }``) can't break the drift guard.
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"could not locate {name} in the demo notebook")


@pytest.mark.parametrize(
    ("notebook_name", "seed_files"),
    [
        ("compose_support_demo.py", SEED_POLICY_FILES),
        ("devops_demo.py", DEVOPS_SEED_FILES),
    ],
)
def test_notebook_files_match_seed(notebook_name: str, seed_files: dict) -> None:
    notebook = _find_notebook(notebook_name)
    if notebook is None:
        pytest.skip(f"{notebook_name} not present in this checkout")

    src = notebook.read_text(encoding="utf-8")
    assert _notebook_dict(src, "_FILES") == seed_files
