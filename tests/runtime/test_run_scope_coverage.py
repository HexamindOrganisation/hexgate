"""Every run entry point must open a ``run_scope``.

A missed boundary is a *silent fail-open*: outside a scope ``get_run_facts()``
returns ``DETACHED``, which reads zeros, so ``run.tool_calls < 20`` would pass
forever and nothing else in the suite would notice.

Hence two halves: :func:`_boundaries` derives the expected set from method
signatures, catching an entry point added later; ``SCOPE_SITES`` pins where each
opens it. The source-text assertions are deliberate — what is guarded against is a
missing line of code.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from collections.abc import Iterator
from typing import Any

import pytest

_CONTEXT_PARAM = "hexgate_context"
_OPENS_SCOPE = "run_scope("
_JOINS_SCOPE = "use_run_facts("
# langchain and pydantic_ai delegate _abind/_bind to these shared helpers instead
# of calling run_scope() inline; test_shared_bind_helpers_open_a_scope pins that
# the helpers do open one.
_DELEGATES_TO_SHARED_BIND = ("abind(", "bind(")
# The shared pre-run seam that fetches the policy and bans, then refuses a
# banned run. The ordering pins below match these as parsed calls, so a
# docstring or comment naming one does not count.
_PREPARES_RUN = frozenset({"aprepare_run", "prepare_run"})
# The seam's keyword for the agent_usage.* refresh, required at every boundary.
_USAGE_KWARG = "usage"
# Anything that starts the run: the identity scope, the run scope, or the
# streamed launch that opens both. ``async with hexgate_context`` is not a call,
# so _first_call matches it separately.
_STARTS_RUN = frozenset(
    {"run_scope", "_abind", "_bind", "_launch_streamed", "sync_scope"}
)
_CHECKS_ADMISSION = "_check_admission"
_API_KEY_KWARG = "api_key"
# Calls that open a run scope, and so pick the sender its run_start span leaves on.
_SCOPE_OPENERS = frozenset({"run_scope", "abind", "bind"})
# The native agent's decisions and usage leave on HEXGATE_API_KEY even when
# load_hexgate_agent(api_key=...) binds another key, so its run start follows them
# there rather than landing in a different project. Lift the exemption only when
# the enforcer and usage handler take the explicit key too.
_ENV_KEY_MODULE = "hexgate.agents.factory"

# (module, class, symbol) of every boundary with an admission gate. Admission must
# precede the scope: entering run_scope is what reports a run, and a refused
# invocation is not one.
ADMISSION_SITES: list[tuple[str, str, str]] = [
    ("hexgate.adapters.google.runner", "HexgateRunner", "run"),
    ("hexgate.adapters.google.runner", "HexgateRunner", "run_async"),
    ("hexgate.adapters.openai.runner", "HexgateRunner", "run"),
    ("hexgate.adapters.openai.runner", "HexgateRunner", "run_sync"),
    ("hexgate.adapters.openai.runner", "HexgateRunner", "_launch_streamed"),
    ("hexgate.agents.factory", "HexgateAgent", "ainvoke"),
    ("hexgate.agents.factory", "HexgateAgent", "astream_events"),
]

# These four take the caller's HexgateContext explicitly, so their boundaries are
# derivable. The native HexgateAgent is ambient, so it is pinned but not derived.
_DERIVABLE = [
    ("hexgate.adapters.langchain.agent", "HexgateLangchainAgent"),
    ("hexgate.adapters.openai.runner", "HexgateRunner"),
    ("hexgate.adapters.pydantic_ai.agent", "HexgatePydanticAgent"),
    ("hexgate.adapters.google.runner", "HexgateRunner"),
]

# (module, class, run method, symbol that must open the scope). Keyed on
# module *and* class because the OpenAI and Google adapters both export a
# class named HexgateRunner. The symbol differs from the method wherever the
# boundary delegates its scope to a shared helper.
SCOPE_SITES: list[tuple[str, str, str, str]] = [
    ("hexgate.adapters.langchain.agent", "HexgateLangchainAgent", "ainvoke", "_abind"),
    ("hexgate.adapters.langchain.agent", "HexgateLangchainAgent", "invoke", "_bind"),
    ("hexgate.adapters.langchain.agent", "HexgateLangchainAgent", "astream", "_abind"),
    ("hexgate.adapters.langchain.agent", "HexgateLangchainAgent", "stream", "_bind"),
    (
        "hexgate.adapters.langchain.agent",
        "HexgateLangchainAgent",
        "astream_events",
        "_abind",
    ),
    ("hexgate.adapters.openai.runner", "HexgateRunner", "run", "run"),
    ("hexgate.adapters.openai.runner", "HexgateRunner", "run_sync", "run_sync"),
    # Both streaming entry points delegate the wrap + scope to _launch_streamed
    # after refreshing the binding and ban gate in their own (sync/async) way.
    (
        "hexgate.adapters.openai.runner",
        "HexgateRunner",
        "run_streamed",
        "_launch_streamed",
    ),
    (
        "hexgate.adapters.openai.runner",
        "HexgateRunner",
        "arun_streamed",
        "_launch_streamed",
    ),
    ("hexgate.adapters.pydantic_ai.agent", "HexgatePydanticAgent", "run", "_abind"),
    ("hexgate.adapters.pydantic_ai.agent", "HexgatePydanticAgent", "run_sync", "_bind"),
    (
        "hexgate.adapters.pydantic_ai.agent",
        "HexgatePydanticAgent",
        "run_stream",
        "_abind",
    ),
    ("hexgate.adapters.pydantic_ai.agent", "HexgatePydanticAgent", "iter", "_abind"),
    ("hexgate.adapters.google.runner", "HexgateRunner", "run", "run"),
    ("hexgate.adapters.google.runner", "HexgateRunner", "run_async", "run_async"),
    # Ambient: no hexgate_context parameter, so _boundaries cannot derive these.
    ("hexgate.agents.factory", "HexgateAgent", "ainvoke", "ainvoke"),
    ("hexgate.agents.factory", "HexgateAgent", "astream_events", "astream_events"),
]


def _load(module_name: str, class_name: str) -> Any:
    return getattr(importlib.import_module(module_name), class_name)


def _boundaries(cls: type) -> set[str]:
    """Public methods taking a ``hexgate_context`` keyword — i.e. run boundaries.
    Derived, not listed, so a new entry point cannot pass by nobody updating a
    constant."""
    found: set[str] = set()
    for name, member in inspect.getmembers(cls, callable):
        if name.startswith("_"):
            continue
        try:
            signature = inspect.signature(member)
        except (TypeError, ValueError):  # pragma: no cover - builtins
            continue
        if _CONTEXT_PARAM in signature.parameters:
            found.add(name)
    return found


def _source_of(module_name: str, class_name: str, symbol: str) -> str:
    return inspect.getsource(getattr(_load(module_name, class_name), symbol))


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _enters_context(node: ast.AST) -> bool:
    return isinstance(node, (ast.With, ast.AsyncWith)) and any(
        isinstance(item.context_expr, ast.Name)
        and item.context_expr.id == _CONTEXT_PARAM
        for item in node.items
    )


def _parsed_nodes(module_name: str, class_name: str, method: str) -> Iterator[ast.AST]:
    source = textwrap.dedent(_source_of(module_name, class_name, method))
    return ast.walk(ast.parse(source))


def _first_call(
    module_name: str,
    class_name: str,
    method: str,
    names: frozenset[str],
    *,
    or_entering_context: bool = False,
) -> tuple[int, int] | None:
    """Position of the earliest real call to one of ``names``, parsed so a
    docstring or comment naming it does not count."""
    positions = [
        (node.lineno, node.col_offset)
        for node in _parsed_nodes(module_name, class_name, method)
        if (isinstance(node, ast.Call) and _called_name(node) in names)
        or (or_entering_context and _enters_context(node))
    ]
    return min(positions, default=None)


@pytest.mark.parametrize(("module_name", "class_name"), _DERIVABLE)
def test_every_adapter_boundary_is_covered(module_name: str, class_name: str) -> None:
    """The guard against a *future* unwired entry point: a new method taking
    ``hexgate_context`` and missing from SCOPE_SITES fails here, rather than
    silently running detached."""
    listed = {
        method
        for module, klass, method, _ in SCOPE_SITES
        if (module, klass) == (module_name, class_name)
    }
    assert _boundaries(_load(module_name, class_name)) == listed


@pytest.mark.parametrize(
    ("module_name", "class_name", "method", "symbol"),
    SCOPE_SITES,
    ids=[f"{m.rsplit('.', 1)[-1]}.{c}.{meth}" for m, c, meth, _ in SCOPE_SITES],
)
def test_scope_is_opened_for_every_boundary(
    module_name: str, class_name: str, method: str, symbol: str
) -> None:
    source = _source_of(module_name, class_name, symbol)
    opens_directly = _OPENS_SCOPE in source
    delegates = any(marker in source for marker in _DELEGATES_TO_SHARED_BIND)
    assert opens_directly or delegates, (
        f"{module_name}.{class_name}.{method} does not open a run scope, "
        f"directly or via the shared abind/bind helpers (expected it in "
        f"{symbol!r}). An unscoped boundary reads DETACHED, so every run.* "
        f"constraint silently passes."
    )


def test_shared_bind_helpers_open_a_scope() -> None:
    """Delegating boundaries trust the helper to open the scope; pin that once
    here rather than per call site."""
    from hexgate.adapters import _common

    for name in ("abind", "bind"):
        source = inspect.getsource(getattr(_common, name))
        assert _OPENS_SCOPE in source, (
            f"hexgate.adapters._common.{name} no longer opens a run scope; "
            f"every adapter boundary delegating to it would silently run "
            f"detached."
        )


def test_scope_opens_after_the_ban_check() -> None:
    """A refused invocation is not a run, so the ban gate must fire first."""
    site = ("hexgate.adapters.langchain.agent", "HexgateLangchainAgent", "ainvoke")
    prepares = _first_call(*site, _PREPARES_RUN)
    binds = _first_call(*site, frozenset({"_abind"}))
    assert prepares is not None and binds is not None
    assert prepares < binds


@pytest.mark.parametrize(
    ("module_name", "class_name", "method"),
    [(module, klass, method) for module, klass, method, _ in SCOPE_SITES],
    ids=[f"{m.rsplit('.', 1)[-1]}.{c}.{meth}" for m, c, meth, _ in SCOPE_SITES],
)
def test_every_boundary_prepares_the_run_before_starting_it(
    module_name: str, class_name: str, method: str
) -> None:
    """Every boundary goes through the shared seam, before anything starts the
    run — so a ban is refused outside the scope, and whatever joins the seam
    later reaches every boundary."""
    prepares = _first_call(module_name, class_name, method, _PREPARES_RUN)
    assert prepares is not None, (
        f"{module_name}.{class_name}.{method} bypasses the aprepare_run / "
        f"prepare_run seam, so it skips the concurrent fetch or the ban gate."
    )
    starts = _first_call(
        module_name, class_name, method, _STARTS_RUN, or_entering_context=True
    )
    assert starts is not None, (
        f"{module_name}.{class_name}.{method} never starts the run"
    )
    assert prepares < starts


def _seam_calls(module_name: str, class_name: str, method: str) -> list[ast.Call]:
    return [
        node
        for node in _parsed_nodes(module_name, class_name, method)
        if isinstance(node, ast.Call) and _called_name(node) in _PREPARES_RUN
    ]


@pytest.mark.parametrize(
    ("module_name", "class_name", "method"),
    [(module, klass, method) for module, klass, method, _ in SCOPE_SITES],
    ids=[f"{m.rsplit('.', 1)[-1]}.{c}.{meth}" for m, c, meth, _ in SCOPE_SITES],
)
def test_every_boundary_passes_the_usage_refresh(
    module_name: str, class_name: str, method: str
) -> None:
    """A boundary that forgets it skips the usage prefetch silently: its quiet
    agent then pays a fail-open first read instead."""
    calls = _seam_calls(module_name, class_name, method)
    assert calls
    for call in calls:
        assert any(kw.arg == _USAGE_KWARG for kw in call.keywords), (
            f"{module_name}.{class_name}.{method} calls the run seam without "
            f"{_USAGE_KWARG}=, so it never refreshes agent_usage.* at run start."
        )


@pytest.mark.parametrize(
    ("module_name", "class_name", "symbol"),
    ADMISSION_SITES,
    ids=[f"{m.rsplit('.', 1)[-1]}.{c}.{s}" for m, c, s in ADMISSION_SITES],
)
def test_scope_opens_after_admission(
    module_name: str, class_name: str, symbol: str
) -> None:
    source = _source_of(module_name, class_name, symbol)
    assert source.index(_CHECKS_ADMISSION) < source.index(_OPENS_SCOPE), (
        f"{module_name}.{class_name}.{symbol} opens its run scope before "
        f"admission, so a refused invocation would be counted as a run."
    )


def _scope_opener_calls(source: str) -> list[ast.Call]:
    """Every direct ``run_scope(...)`` / ``abind(...)`` / ``bind(...)`` call —
    by AST, since ``"bind("`` as text also matches ``def _abind(``."""
    return [
        node
        for node in ast.walk(ast.parse(textwrap.dedent(source)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _SCOPE_OPENERS
    ]


_KEYED_SITES = sorted(
    {(m, c, symbol) for m, c, _, symbol in SCOPE_SITES if m != _ENV_KEY_MODULE}
)


@pytest.mark.parametrize(
    ("module_name", "class_name", "symbol"),
    _KEYED_SITES,
    ids=[f"{m.rsplit('.', 1)[-1]}.{c}.{s}" for m, c, s in _KEYED_SITES],
)
def test_boundaries_forward_the_api_key(
    module_name: str, class_name: str, symbol: str
) -> None:
    """A boundary that drops the key reports its runs on the env key's sender —
    another project's quota, or none."""
    calls = _scope_opener_calls(_source_of(module_name, class_name, symbol))
    assert calls, f"{module_name}.{class_name}.{symbol} opens no scope directly"
    for call in calls:
        keywords = {keyword.arg for keyword in call.keywords}
        assert _API_KEY_KWARG in keywords, (
            f"{module_name}.{class_name}.{symbol} opens a scope without api_key="
        )


def test_shared_bind_helpers_forward_the_api_key() -> None:
    from hexgate.adapters import _common

    for name in ("abind", "bind"):
        [call] = _scope_opener_calls(inspect.getsource(getattr(_common, name)))
        assert _API_KEY_KWARG in {keyword.arg for keyword in call.keywords}


@pytest.mark.parametrize("method", ["run_streamed", "arun_streamed"])
def test_streamed_boundaries_launch_after_the_ban_check(method: str) -> None:
    """The streaming boundaries own only the refresh + ban gate; the scope lives
    in ``_launch_streamed``, so each must actually hand off to it — and only
    once a banned run has been refused."""
    site = ("hexgate.adapters.openai.runner", "HexgateRunner", method)
    prepares = _first_call(*site, _PREPARES_RUN)
    launches = _first_call(*site, frozenset({"_launch_streamed"}))
    assert launches is not None
    assert prepares is not None and prepares < launches


def test_run_streamed_rejoins_rather_than_mints() -> None:
    """``run_streamed`` hands tools to a background task that snapshots the
    contextvars, so the consumer-side iterator must re-bind the same object —
    minting would split one invocation across two run ids."""
    source = _source_of(
        "hexgate.adapters.openai.runner", "HexgateRunner", "_launch_streamed"
    )
    assert _OPENS_SCOPE in source
    assert _JOINS_SCOPE in source
    # The scope must wrap the run_streamed call itself, since that is where the
    # background task snapshots the context.
    assert source.index(_OPENS_SCOPE) < source.index("Runner.run_streamed(")
    assert source.index("Runner.run_streamed(") < source.index(_JOINS_SCOPE)
