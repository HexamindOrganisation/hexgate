"""Fail when the SDK grows something the user docs never mention.

Each check lists what the code exposes (policy keys, CLI commands and flags,
public exports, environment variables) and looks for it in the Mintlify pages
under ``docs/`` (``*.mdx`` only: ``internals/`` and ``adr/`` are ``.md`` and
written for contributors, so they don't count).

What is undocumented today sits in a ``KNOWN_*`` set. A new gap fails the
suite; so does a known one that has since been documented, so the sets only
ever shrink. To fix a failure, document the item, or (if it really is
internal) add it to the set with a reason in the commit message.
"""

from __future__ import annotations

import argparse
import ast
import re
import types
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel

import hexgate
from hexgate.cli import _build_parser
from hexgate.security.models import AgentPolicy

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"

# What the docs don't cover yet. Remove an entry when you document it.
KNOWN_POLICY_KEYS = {
    "allowed_paths",
    "denied_paths",
    "enabled",
    "file_scope",
}
KNOWN_COMMANDS: set[str] = set()
KNOWN_FLAGS = {
    "register --force",
    "register --register-subagents",
    "serve --no-bind-subagents",
    "serve --register-subagents",
    "serve --register-subagents-force",
}
KNOWN_EXPORTS = {
    "ContextAttributeValue",
    "HexgateClient",
    "HexgateConfig",
    "SkillDefinition",
    "SkillResources",
    "SubagentEdge",
    "SubagentLink",
    "attach_guards",
    "clear_registered_agents",
    "enumerate_subagents",
    "list_available_agents",
    "list_local_agents",
    "list_registered_agents",
    "unregister_agent_factory",
}
KNOWN_ENV_VARS = {
    # Read by the platform usage source (#331), which nothing calls yet:
    # document them when agent_usage.* counts come from the platform.
    "HEXGATE_USAGE_MAX_STALENESS_SECONDS",
    "HEXGATE_USAGE_REFRESH_SECONDS",
    "HEXGATE_USAGE_SYNC_AFTER_SECONDS",
}

ENV_VAR = re.compile(r"HEXGATE_[A-Z0-9_]*[A-Z0-9]")


def _pages(subdir: str = "") -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((DOCS / subdir).rglob("*.mdx"))
    )


def _models(tp: object, seen: set[type]) -> Iterator[type[BaseModel]]:
    """Every pydantic model reachable from ``tp``, through unions, lists and dicts."""
    for arg in typing.get_args(tp):
        yield from _models(arg, seen)
    if isinstance(tp, type) and not isinstance(tp, types.GenericAlias):
        if issubclass(tp, BaseModel) and tp not in seen:
            seen.add(tp)
            yield tp
            for field in tp.model_fields.values():
                yield from _models(field.annotation, seen)


def _policy_keys() -> set[str]:
    return {
        field.alias or name
        for model in _models(AgentPolicy, set())
        for name, field in model.model_fields.items()
    }


def _subcommands(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[str, argparse.ArgumentParser]]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                path = (*prefix, name)
                yield " ".join(path), sub
                yield from _subcommands(sub, path)


def _commands() -> set[str]:
    return {cmd for cmd, _ in _subcommands(_build_parser())}


def _flags() -> set[str]:
    return {
        f"{cmd} {opt}"
        for cmd, sub in _subcommands(_build_parser())
        for action in sub._actions
        for opt in action.option_strings
        if opt.startswith("--") and opt != "--help"
    }


def _env_vars() -> set[str]:
    """``HEXGATE_*`` names the SDK reads: string literals that are exactly a
    name, so a docstring that merely mentions one doesn't count."""
    found: set[str] = set()
    for path in (REPO / "hexgate").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if ENV_VAR.fullmatch(node.value):
                    found.add(node.value)
    return found


def _flag_documented(flag: str, _corpus: str) -> bool:
    """A flag counts only on its own command's page (``docs/cli/<command>.mdx``):
    ``keygen --force`` must not document ``register --force``."""
    cmd, opt = flag.rsplit(" ", 1)
    page = DOCS / "cli" / f"{cmd.split()[0]}.mdx"
    return page.exists() and _term_documented(opt, page.read_text(encoding="utf-8"))


def _key_documented(key: str, text: str) -> bool:
    # A key counts when the docs show it as code or as a YAML key.
    return re.search(rf"`{re.escape(key)}`|\b{re.escape(key)}:", text) is not None


def _word_documented(word: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _term_documented(term: str, text: str) -> bool:
    # Whole term only: `--manifest` must not count as documented by `--manifesto`.
    return re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text) is not None


CHECKS = {
    "policy keys": (_policy_keys, lambda: _pages(), _key_documented, KNOWN_POLICY_KEYS),
    "CLI commands": (
        _commands,
        lambda: _pages("cli"),
        lambda c, t: _term_documented(f"hexgate {c}", t),
        KNOWN_COMMANDS,
    ),
    "CLI flags": (
        _flags,
        lambda: _pages("cli"),
        _flag_documented,
        KNOWN_FLAGS,
    ),
    "public exports": (
        lambda: set(hexgate.__all__),
        lambda: _pages(),
        _word_documented,
        KNOWN_EXPORTS,
    ),
    "environment variables": (
        _env_vars,
        lambda: (DOCS / "reference" / "environment.mdx").read_text(encoding="utf-8"),
        _word_documented,
        KNOWN_ENV_VARS,
    ),
}


@pytest.mark.parametrize("check", list(CHECKS))
def test_no_new_undocumented(check: str) -> None:
    items, text, documented, known = CHECKS[check]
    corpus = text()
    missing = {item for item in items() if not documented(item, corpus)}
    new = sorted(missing - known)
    assert not new, (
        f"undocumented {check}: {new}. Document them in docs/ (see this test's "
        "docstring for where each check looks)."
    )


@pytest.mark.parametrize("check", list(CHECKS))
def test_known_gaps_are_still_gaps(check: str) -> None:
    items, text, documented, known = CHECKS[check]
    corpus = text()
    present = items()
    stale = sorted(k for k in known if k not in present or documented(k, corpus))
    assert not stale, (
        f"{stale} no longer need to be in the known gaps for {check}: remove them "
        "from the KNOWN_* set in tests/docs/test_docs_coverage.py."
    )
