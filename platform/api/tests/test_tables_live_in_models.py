"""Tripwire: every table is declared in ``hexgate_api/models.py``.

api-init imports only ``hexgate_api.models`` before ``create_all``
(``platform/docker-compose.deploy.yml``), so a ``table=True`` model declared in
a feature slice is never created in deploy. Nothing else fails: under pytest
the app import registers the table anyway, and every test passes.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "hexgate_api"


def test_when_a_module_declares_a_table_then_it_is_models_py() -> None:
    declaring = {
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if "table=True" in path.read_text()
    }

    assert declaring == {"models.py"}
