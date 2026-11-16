"""Fail when the SDK grows something the user docs never mention.

Each check lists what the code exposes (policy keys, CLI commands and flags,
public exports, environment variables) and looks for it in the Mintlify pages
under ``docs/`` (``*.mdx`` only: ``internals/`` and ``adr/`` are ``.md`` and
written for contributors, so they don't count, and ``node_modules`` is skipped).

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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

import hexgate
from hexgate.cli import _build_parser
from hexgate.security.compose.grammar import Entry
from hexgate.security.models import AgentPolicy

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"

# What the docs don't cover yet. Remove an entry when you document it.
KNOWN_POLICY_KEYS = {
    # The authoring grammar (hexgate/security/compose/grammar.py): boundary,
    # reach, mcp, import/export and the agents/roles bodies. Explained only in
    # docs/internals/policy-modules.md so far.
    "agents.admission",
    "agents.boundary",
    "agents.constraints",
    "agents.guards",
    "agents.import",
    "agents.mcp",
    "agents.reach",
    "agents.roles",
    "agents.skills",
    "agents.tools",
    "agents.usage",
    "boundary",
    "boundary.admission",
    "boundary.reach",
    "boundary.skills",
    "boundary.tools",
    "export",
    "export.admission",
    "export.boundary",
    "export.import",
    "export.mcp",
    "export.reach",
    "export.skills",
    "export.tools",
    "import",
    "mcp",
    "mcp.constraints",
    "mcp.mode",
    "reach",
    "reach.as",
    "reach.constraints",
    "reach.mode",
    "roles.boundary",
    "roles.import",
    "roles.mcp",
    "roles.reach",
}
KNOWN_COMMANDS: set[str] = set()
KNOWN_FLAGS: set[str] = set()
KNOWN_EXPORTS: set[str] = set()
KNOWN_ENV_VARS: set[str] = set()

ENV_VAR = re.compile(r"HEXGATE_[A-Z0-9_]*[A-Z0-9]")
HEADING = re.compile(r"(#{1,6}) ")
FENCE = "```"
YAML_BLOCK = re.compile(r"```ya?ml[^\n]*\n(.*?)```", re.DOTALL)


# ---- reading the docs ------------------------------------------------------


def _pages(subdir: str = "") -> list[Path]:
    return sorted(
        p
        for p in (DOCS / subdir).rglob("*.mdx")
        # Relative parts only: a checkout under a dot folder (.claude/worktrees/…)
        # must still see its pages.
        if not any(
            part == "node_modules" or part.startswith(".")
            for part in p.relative_to(DOCS).parts
        )
    )


def _text(subdir: str = "") -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _pages(subdir))


def _term_documented(term: str, text: str) -> bool:
    # Whole term only: `--manifest` must not count as documented by `--manifesto`.
    return re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text) is not None


def _key_documented(key: str, text: str) -> bool:
    # A key counts when the docs show it as code or as a YAML key.
    return re.search(rf"`{re.escape(key)}`|\b{re.escape(key)}:", text) is not None


def _code_documented(name: str, text: str) -> bool:
    """A Python name counts when the docs show it as code: `` `name` ``,
    ``name(`` or a line of an import list. A bare word does not, so an export
    named ``bash`` is not documented by every ```` ```bash ```` fence."""
    n = re.escape(name)
    return (
        re.search(
            rf"(?<!`)`{n}[`(.\[ ]|(?<![\w.]){n}\(|^\s*{n},?\s*$|import [^\n]*\b{n}\b",
            text,
            re.MULTILINE,
        )
        is not None
    )


def _sections(text: str) -> Iterator[tuple[str, str]]:
    """``(heading, body)`` per heading, ignoring ``#`` lines inside code fences.

    A section runs to the next heading of the same or a higher level, so a
    ``###`` inside a ``##`` belongs to both."""
    lines = text.splitlines()
    headings: list[tuple[int, int]] = []  # (line index, level)
    in_fence = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(FENCE):
            in_fence = not in_fence
        elif not in_fence and (match := HEADING.match(line)):
            headings.append((index, len(match.group(1))))
    for position, (start, level) in enumerate(headings):
        end = next(
            (i for i, lvl in headings[position + 1 :] if lvl <= level), len(lines)
        )
        yield lines[start], "\n".join(lines[start + 1 : end])


def _command_lines(text: str) -> list[str]:
    """Lines of text with shell continuations (a trailing ``\\``) joined."""
    return re.sub(r"\\\n\s*", " ", text).splitlines()


# ---- what the code exposes -------------------------------------------------


def _keys_under(
    tp: object, parent: str | None, seen: set[tuple[type, str | None]], out: set[str]
) -> None:
    """Add ``parent.key`` for every field of every model reachable from ``tp``
    (through unions, lists and dicts), or the bare ``key`` at the top level.

    ``parent`` is the nearest named field above, skipping the name-maps in
    between: ``mode`` under ``tools: {refund: {mode: …}}`` is ``tools.mode``."""
    for arg in typing.get_args(tp):
        _keys_under(arg, parent, seen, out)
    if not (isinstance(tp, type) and not isinstance(tp, types.GenericAlias)):
        return
    if not issubclass(tp, BaseModel) or (tp, parent) in seen:
        return
    seen.add((tp, parent))
    for name, field in tp.model_fields.items():
        key = field.alias or name
        out.add(f"{parent}.{key}" if parent else key)
        _keys_under(field.annotation, key, seen, out)


def _policy_keys() -> set[str]:
    """Keys of the resolved policy model and of the authoring grammar, each with
    the field it sits under: a nested ``mode`` must be documented as such."""
    out: set[str] = set()
    seen: set[tuple[type, str | None]] = set()
    _keys_under(AgentPolicy, None, seen, out)
    _keys_under(Entry, None, seen, out)
    return out


def _yaml_keys() -> set[tuple[tuple[str, ...], str]]:
    """``(ancestors, key)`` for every key in every YAML example in the docs."""
    found: set[tuple[tuple[str, ...], str]] = set()

    def walk(node: object, ancestors: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                found.add((ancestors, str(key)))
                walk(value, (*ancestors, str(key)))
        elif isinstance(node, list):
            for value in node:
                walk(value, ancestors)

    for page in _pages():
        for block in YAML_BLOCK.findall(page.read_text(encoding="utf-8")):
            try:
                walk(yaml.safe_load(block), ())
            except yaml.YAMLError:
                continue
    return found


def _key_corpus() -> tuple[set[tuple[tuple[str, ...], str]], str]:
    return _yaml_keys(), _text()


def _key_documented(
    item: str, corpus: tuple[set[tuple[tuple[str, ...], str]], str]
) -> bool:
    """A top-level key counts as a top-level YAML key or as `` `key` ``. A nested
    one counts inside a YAML example under its parent, or written out as
    ``parent.key`` (``parent.<name>.key`` through a name-map)."""
    yaml_keys, text = corpus
    parent, _, key = item.rpartition(".")
    if not parent:
        return ((), key) in yaml_keys or f"`{key}`" in text
    if any(k == key and parent in ancestors for ancestors, k in yaml_keys):
        return True
    path = rf"{re.escape(parent)}\.(?:[\w*<>-]+\.)?{re.escape(key)}(?![\w-])"
    return re.search(path, text) is not None


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


# ---- where each flag may be documented -------------------------------------


def _flag_corpus() -> dict[str, str]:
    """For each command, the text that may document its flags.

    A top-level command (``serve``) has its own page, so the whole page counts. A
    subcommand (``policy check``) shares its page with its siblings, so only the
    sections whose heading opens with it (`` `check` ``) and the command lines that
    run it (``hexgate policy check …``) count: ``test --role`` must not document
    ``check --role``.
    """
    corpus: dict[str, str] = {}
    for cmd in _commands():
        top, *rest = cmd.split()
        page = DOCS / "cli" / f"{top}.mdx"
        if not page.exists():
            corpus[cmd] = ""
            continue
        text = page.read_text(encoding="utf-8")
        if not rest:
            corpus[cmd] = text
            continue
        sub = rest[-1]
        # Only a heading that opens with the subcommand as code (`` ### `check` ``)
        # is that subcommand's: one that merely mentions it covers several.
        own = re.compile(rf"#+ `(?:{re.escape(top)} )?{re.escape(sub)}`")
        parts = [body for heading, body in _sections(text) if own.match(heading)]
        parts += [
            line
            for line in _command_lines(text)
            if _term_documented(f"hexgate {cmd}", line)
        ]
        corpus[cmd] = "\n".join(parts)
    return corpus


def _flag_documented(flag: str, corpus: dict[str, str]) -> bool:
    cmd, opt = flag.rsplit(" ", 1)
    return _term_documented(opt, corpus.get(cmd, ""))


# ---- the checks ------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    items: Callable[[], set[str]]
    corpus: Callable[[], typing.Any]
    documented: Callable[[str, typing.Any], bool]
    known: set[str]


CHECKS = {
    "policy keys": Check(_policy_keys, _key_corpus, _key_documented, KNOWN_POLICY_KEYS),
    "CLI commands": Check(
        _commands,
        lambda: _text("cli"),
        lambda cmd, text: _term_documented(f"hexgate {cmd}", text),
        KNOWN_COMMANDS,
    ),
    "CLI flags": Check(_flags, _flag_corpus, _flag_documented, KNOWN_FLAGS),
    "public exports": Check(
        lambda: set(hexgate.__all__), _text, _code_documented, KNOWN_EXPORTS
    ),
    "environment variables": Check(
        _env_vars,
        lambda: (DOCS / "reference" / "environment.mdx").read_text(encoding="utf-8"),
        _term_documented,
        KNOWN_ENV_VARS,
    ),
}


@pytest.mark.parametrize("name", list(CHECKS))
def test_no_new_undocumented(name: str) -> None:
    check = CHECKS[name]
    corpus = check.corpus()
    missing = {item for item in check.items() if not check.documented(item, corpus)}
    new = sorted(missing - check.known)
    assert not new, (
        f"undocumented {name}: {new}. Document them in docs/ (see this test's "
        "docstring for where each check looks)."
    )


@pytest.mark.parametrize("name", list(CHECKS))
def test_known_gaps_are_still_gaps(name: str) -> None:
    check = CHECKS[name]
    corpus = check.corpus()
    present = check.items()
    stale = sorted(
        k for k in check.known if k not in present or check.documented(k, corpus)
    )
    assert not stale, (
        f"{stale} no longer need to be in the known gaps for {name}: remove them "
        "from the KNOWN_* set in tests/docs/test_docs_coverage.py."
    )
