"""Which ``boundary:`` blocks a set of draft files loosens or tightens.

``/policy/diff`` compares composed grants; it never looks at a boundary block on
its own, so a ceiling raised where no grant reaches it yet shows no change there.
This module compares the blocks themselves, file by file, at every level a block
may sit: the top level, an agent body, a role body, an export.

A boundary block is closed-world (the lowering gives it ``default_policy: deny``),
and in a ceiling a ``deny`` entry, conditional or not, leaves the tool ineligible
exactly as leaving it out does. So for a tool an absent entry and a ``deny``
entry are the same thing here, and only a move between deny,
``approval_required`` and ``allow``, or a change to what a permitted entry lets
through, is reported. Admission, reach and skill entries differ: their gates
engage only while some key of their kind is present (the linker keeps them as
explicit denies for that reason), so such an entry leaving the block, or a
reach transfer mode leaving its entry, may switch its gate off and is always
``widened``; a ``deny`` one arriving is ``tightened``. Whenever one entry both
loosens and tightens, it is reported ``widened``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Mapping
from typing import Any, Literal

from pydantic import BaseModel

from hexgate_api.features.policy_modules.risk_facts import _MIRROR

WIDENED = "widened"
TIGHTENED = "tightened"
Kind = Literal["widened", "tightened"]
# A block or entry's place in a file: ("agents", "bot", "boundary", "tools", "x").
BlockPath = tuple[str, ...]

# The name-map entries of a BoundaryBlock; ``admission`` is its one single entry.
_SECTIONS = ("tools", "reach", "skills")
# The entries whose gate engages only while a key of their kind is listed.
_GATED = ("admission", "reach", "skills")
# Numeric bounds, op -> inclusive.
_UPPER_BOUND = {"<": False, "<=": True}
_LOWER_BOUND = {">": False, ">=": True}


class BoundaryChange(BaseModel):
    """One boundary block, or one entry of it, that a draft changes."""

    file: str
    path: str
    kind: Kind
    before: dict[str, Any] | None
    after: dict[str, Any] | None


def boundary_changes(
    saved: Mapping[str, str], drafted: Mapping[str, str]
) -> list[BoundaryChange]:
    """Every boundary change from ``saved`` to ``drafted`` (``{name: text}``).

    A file whose text is the same on both sides is not parsed. A changed file
    is parsed on both sides, and either side that does not parse raises the
    SDK's ``LinkError``.
    """
    changes: list[BoundaryChange] = []
    for name in sorted(saved.keys() | drafted.keys()):
        old, new = saved.get(name), drafted.get(name)
        if old != new:
            changes += _file_changes(name, _blocks(old, name), _blocks(new, name))
    return changes


def _blocks(text: str | None, name: str) -> dict[BlockPath, Any]:
    """``{path: BoundaryBlock}`` for one file; empty when the file is absent.
    Paths are tuples, so an agent named ``a.roles.b`` never shares a key with
    role ``b`` of agent ``a``."""
    from hexgate.security.compose import parse_entry

    if text is None:
        return {}
    scopes = _scopes(parse_entry(text, source=name))
    return {(*p, "boundary"): s.boundary for p, s in scopes if s.boundary is not None}


def _scopes(entry) -> Iterator[tuple[BlockPath, Any]]:
    """Every scope of a parsed file that may hold a boundary, by path."""
    yield (), entry
    for agent, body in entry.agents.items():
        yield ("agents", agent), body
        for role, role_body in body.roles.items():
            yield ("agents", agent, "roles", role), role_body
    for export, role_body in entry.exports.items():
        yield ("export", export), role_body


def _file_changes(
    file: str, old: dict[BlockPath, Any], new: dict[BlockPath, Any]
) -> list[BoundaryChange]:
    """A block added is a new ceiling (tightened); a block removed takes a
    ceiling away (widened); a block on both sides is compared entry by entry."""
    changes = []
    for path in sorted(old.keys() | new.keys()):
        a, b = old.get(path), new.get(path)
        if a is None or b is None:
            kind = TIGHTENED if a is None else WIDENED
            changes.append(_change(file, path, kind, a, b))
        else:
            changes += _block_changes(file, path, a, b)
    return changes


@dataclasses.dataclass(frozen=True)
class _Collision:
    """Several raw reach names of one block that lower to one target, in order."""

    entries: tuple[tuple[str, Any], ...]

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        return {name: spec.model_dump(**kwargs) for name, spec in self.entries}


def _entries(block) -> dict[BlockPath, Any]:
    """A block's entries keyed by their path inside the block.

    Reach targets are keyed the way the lowering names them, by
    ``canonical_name`` (hexgate/security/models.py:521), which trims and maps a
    blank name to ``default``; two raw names on one target become a
    :class:`_Collision`. Tools need no such key: the lowering uses their names
    as written (hexgate/security/compose/lower.py:93). Skills cannot collide:
    ``SkillPolicy`` rejects two names that trim to one (models.py:464-470)."""
    from hexgate.security.naming import canonical_name

    entries: dict[BlockPath, Any] = {
        (section, name): spec
        for section in ("tools", "skills")
        for name, spec in getattr(block, section).items()
    }
    targets: dict[str, list[tuple[str, Any]]] = {}
    for name, spec in block.reach.items():
        targets.setdefault(canonical_name(name), []).append((name, spec))
    for target, named in targets.items():
        entries[("reach", target)] = (
            named[0][1] if len(named) == 1 else _Collision(tuple(named))
        )
    if block.admission is not None:
        entries[("admission",)] = block.admission
    return entries


def _block_changes(file: str, path: BlockPath, a, b) -> list[BoundaryChange]:
    old, new = _entries(a), _entries(b)
    changes = []
    for key in sorted(old.keys() | new.keys()):
        before, after = old.get(key), new.get(key)
        if isinstance(before, _Collision) or isinstance(after, _Collision):
            # Which raw name wins is decided per lowered key, not per entry, so
            # a collision is not compared: anything but no change is widened.
            kind = None if before == after else WIDENED
        elif key[0] in _GATED and (before is None) != (after is None):
            kind = _gate_entry_kind(before, after)
        else:
            kind = _entry_kind(before, after)
        if kind is not None:
            changes.append(_change(file, (*path, *key), kind, before, after))
    return changes


def _change(file: str, path: BlockPath, kind: Kind, before, after) -> BoundaryChange:
    """The wire change; the path is dotted only here, for display."""
    return BoundaryChange(
        file=file,
        path=".".join(path),
        kind=kind,
        before=_dump(before),
        after=_dump(after),
    )


def _looseness(spec) -> int:
    """How much a ceiling entry lets through by its mode; absent is a deny."""
    from hexgate.security.analyzer import MODE_STRICTNESS

    return -MODE_STRICTNESS["deny" if spec is None else spec.mode]


def _gate_entry_kind(before, after) -> Kind:
    """A gate's entry leaving may disengage the gate; one arriving may engage it,
    and only a ``deny`` one is sure to let nothing more through."""
    return TIGHTENED if before is None and after.mode == "deny" else WIDENED


def _entry_kind(before, after) -> Kind | None:
    """How one ceiling entry moved, or None when it lets through the same."""
    if before == after:
        return None
    was, now = _looseness(before), _looseness(after)
    deny = _looseness(None)
    if was == deny or now == deny:
        return _order_kind(was, now)
    found = {
        _order_kind(was, now),
        _via_kind(before, after),
        *_constraint_kinds(before.constraints, after.constraints),
    }
    return WIDENED if WIDENED in found else TIGHTENED if TIGHTENED in found else None


def _order_kind(was: Any, now: Any) -> Kind | None:
    """Two values where larger lets more through."""
    return None if now == was else WIDENED if now > was else TIGHTENED


def _via_kind(before, after) -> Kind | None:
    """Reach and skill entries cap the transfer modes or levels they list. Each
    reach mode lowers to its own key under its own gate (as-tool, handoff), so a
    mode leaving may switch that gate off, as an entry leaving does."""
    from hexgate.security.compose.grammar import ReachSpec

    if not hasattr(before, "via"):
        return None
    was, now = frozenset(before.via), frozenset(after.via)
    if isinstance(before, ReachSpec):
        return None if was == now else WIDENED
    return _set_kind(was, now)


def _set_kind(was: frozenset, now: frozenset) -> Kind | None:
    """An allowlist: anything new on it widens, only removals tighten."""
    if now == was:
        return None
    return TIGHTENED if now < was else WIDENED


# --- constraints --------------------------------------------------------------
# A permitted entry's constraints all must hold. Each condition becomes a fact
# under a key; a key on both sides is compared by its family, a key only before
# is a condition removed (widened), a key only after one added (tightened).


def _constraint_kinds(before: list[str], after: list[str]) -> set[Kind | None]:
    was, now = _facts(before), _facts(after)
    kinds: set[Kind | None] = set()
    for key in was.keys() | now.keys():
        if key not in now:
            kinds.add(WIDENED)
        elif key not in was:
            kinds.add(TIGHTENED)
        else:
            kinds.add(_fact_kind(key[0], was[key], now[key]))
    return kinds


def _facts(constraints: list[str]) -> dict[tuple[str, str], Any]:
    """``{(family, subject): value}``. Families: ``upper`` and ``lower`` (a
    numeric bound, the tightest of its kind kept), ``in`` and ``not in`` (a
    literal list, combined), ``text`` (any other condition, matched as written)."""
    from hexgate.security.constraints import conjuncts

    facts: dict[tuple[str, str], Any] = {}
    for condition in (c for s in constraints for c in conjuncts(s)):
        family, subject, value = _fact(condition)
        key = (family, subject)
        facts[key] = _combine(family, facts[key], value) if key in facts else value
    return facts


def _fact(condition: str) -> tuple[str, str, Any]:
    from hexgate.security.constraints import (
        Cmp,
        ConstraintParseError,
        Lit,
        parse_constraint,
    )

    try:
        node = parse_constraint(condition)
    except ConstraintParseError:
        return "text", condition.strip(), None
    if not isinstance(node, Cmp):
        return "text", repr(_tree(node)), None
    left, op, right = node.left, node.op, node.right
    if isinstance(left, Lit) and not isinstance(right, Lit) and op in _MIRROR:
        left, op, right = right, _MIRROR[op], left
    if isinstance(right, Lit) and not isinstance(left, Lit):
        fact = _literal_fact(repr(_tree(left)), op, right.value)
        if fact is not None:
            return fact
    return "text", repr((_tree(left), op, _tree(right))), None


def _tree(node: Any) -> Any:
    """A parsed condition without its source text, so two spellings of one
    condition (spacing, parentheses) compare equal and literals stay exact."""
    if dataclasses.is_dataclass(node):
        return (
            type(node).__name__,
            *(
                _tree(getattr(node, f.name))
                for f in dataclasses.fields(node)
                if f.name != "source"
            ),
        )
    if isinstance(node, tuple):
        return tuple(_tree(part) for part in node)
    return node


def _literal_fact(subject: str, op: str, value: Any) -> tuple[str, str, Any] | None:
    """Bounds are stored so that a larger value lets more through. An infinite
    literal (``1e999``) is a bound too: it orders past every finite cap."""
    number = isinstance(value, int | float) and not isinstance(value, bool)
    if number and op in _UPPER_BOUND:
        return "upper", subject, (value, _UPPER_BOUND[op])
    if number and op in _LOWER_BOUND:
        return "lower", subject, (-value, _LOWER_BOUND[op])
    if op in ("==", "!="):  # one value allowed, or one excluded
        op, value = ("in" if op == "==" else "not in"), [value]
    if op in ("in", "not in") and isinstance(value, list):
        return op, subject, frozenset(_json_key(v) for v in value)
    return None


def _json_key(value: Any) -> Any:
    """A hashable key two literals share exactly when the enforced (Rego)
    equality holds: ``5`` and ``5.0`` are one value, decided by Python ``==``
    so large integers stay exact, and a bool is never a number, inside list and
    dict literals too. At the top level this is also the SDK's ``_json_eq``;
    inside a list that one uses Python ``==``, where ``[true]`` equals ``[1]``."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int | float):
        return ("number", value)
    if isinstance(value, list):
        return ("list", tuple(_json_key(v) for v in value))
    if isinstance(value, dict):
        return ("dict", frozenset((k, _json_key(v)) for k, v in value.items()))
    return ("scalar", value)


def _combine(family: str, a: Any, b: Any) -> Any:
    """Two conditions of one family on one subject both hold."""
    if family in ("upper", "lower"):
        return min(a, b)
    if family == "in":
        return a & b
    if family == "not in":
        return a | b
    return a


def _fact_kind(family: str, was: Any, now: Any) -> Kind | None:
    if family in ("upper", "lower"):
        return _order_kind(was, now)
    if family == "in":
        return _set_kind(was, now)
    if family == "not in":
        return _set_kind(now, was)  # an exclusion list shrinking lets more through
    return None


def _dump(spec) -> dict[str, Any] | None:
    return None if spec is None else spec.model_dump(mode="json", by_alias=True)
