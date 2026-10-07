"""RunStartEvent's wire shape, and the import direction that keeps
``hexgate.tracing.runs`` out of a cycle with ``hexgate.runtime.run_facts``."""

from __future__ import annotations

import ast
from datetime import timezone
from pathlib import Path

import hexgate.tracing.runs as runs_mod
from hexgate.tracing import semconv
from hexgate.tracing.runs import RunStartEvent

_RUNTIME_PACKAGE = "hexgate.runtime"


def test_span_attributes_are_exactly_the_envelope_plus_run_id() -> None:
    event = RunStartEvent(
        agent_name="agent", run_id="run-1", session_id="sess", user_id="alice"
    )

    assert event.span_attributes() == {
        semconv.EVENT_ID: str(event.event_id),
        semconv.AGENT_NAME: "agent",
        semconv.SESSION_ID: "sess",
        semconv.USER_ID: "alice",
        semconv.RUN_ID: "run-1",
    }


def test_scope_is_the_runs_scope() -> None:
    assert RunStartEvent.SCOPE == semconv.SCOPE_RUNS


def test_occurred_at_is_timezone_aware_utc() -> None:
    event = RunStartEvent(agent_name="agent", run_id="run-1")

    assert event.occurred_at.tzinfo is timezone.utc


def test_module_never_imports_the_runtime_package() -> None:
    """run_facts imports this module; importing hexgate.runtime back from here
    would re-enter a half-initialised run_facts and raise ImportError."""
    tree = ast.parse(Path(runs_mod.__file__).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)

    assert not [name for name in imported if name.startswith(_RUNTIME_PACKAGE)]
