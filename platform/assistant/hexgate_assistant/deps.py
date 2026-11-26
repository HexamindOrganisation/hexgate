"""What one turn's tools act with: the user's token, the files as the turn
found them, and what the write tool counts."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Applied:
    """A saved change and the tool call that saved it."""

    change_id: str
    call_id: str


@dataclass
class TurnWrites:
    """What `apply_change` knows and counts within one turn."""

    project_id: str
    session_id: str | None
    # Called with the `change` event's value once a change is saved.
    on_change: Callable[[dict[str, Any]], None]
    # Where this turn's messages start in the run's history: a read made
    # before it was made in an earlier turn.
    turn_start: int = 0
    attempts: int = 0
    # The change this turn applied, once it did.
    applied: "Applied | None" = None
    # Why writing ended for the rest of the turn, as the model is told.
    ended: str | None = None
    # One call at a time: two parallel calls would both pass the counters.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True)
class TurnDeps:
    """What one turn's tools act with."""

    # The user's token for this message. Out of repr: a logged or stored
    # repr of the deps must never carry it.
    token: str = field(repr=False)
    # {file name: text_hash} as the turn found the files: the base every
    # write of this turn is checked against.
    snapshot: Mapping[str, str] = field(default_factory=dict)
    # What `apply_change` counts this turn; None when the token may not
    # write, and it then refuses every call.
    writes: TurnWrites | None = None
