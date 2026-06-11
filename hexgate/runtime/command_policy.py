"""Static command-allowlist policy for the workspace bash tool.

This is an *intent-shaping* layer, not a security boundary. The actual
security boundary is the OS-level sandbox (e.g. ``srt``). The policy here
parses a shell command string and rejects it if any externally-invoked
program is not on the configured allowlist, or if the command contains
constructs that defeat static analysis.

Once a scripting language (``python``, ``bash``, ``sh``, ``node``, ``perl``,
``awk`` …) is on the allowlist, the agent can execute arbitrary code through
``-c``-style flags. Treat the allowlist as a way to constrain reach, not as
a primitive that can contain a determined adversary.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import bashlex
import bashlex.errors

# Built-in shell commands that don't exec anything external. These are
# always permitted, regardless of the configured allowlist.
SHELL_BUILTINS: frozenset[str] = frozenset(
    {
        ":",
        "[",
        "]",
        "alias",
        "bg",
        "break",
        "cd",
        "continue",
        "declare",
        "echo",
        "exit",
        "export",
        "false",
        "fg",
        "hash",
        "help",
        "history",
        "jobs",
        "let",
        "local",
        "printf",
        "pwd",
        "read",
        "readonly",
        "return",
        "set",
        "shift",
        "test",
        "times",
        "trap",
        "true",
        "type",
        "ulimit",
        "umask",
        "unalias",
        "unset",
        "wait",
    }
)

# Commands that interpret an arbitrary string or file as code, defeating
# static analysis. Always rejected, even if explicitly allowlisted.
ALWAYS_REJECTED: frozenset[str] = frozenset({"eval", "source", "."})

# Wrappers that defer to a target program. We look through them to the
# real executable so that ``exec curl …`` is checked as ``curl``.
PREFIX_COMMANDS: frozenset[str] = frozenset({"command", "env", "exec"})


@dataclass(slots=True, frozen=True)
class Allowed:
    """The command is permitted by policy."""


@dataclass(slots=True, frozen=True)
class Rejected:
    """The command is denied. ``offending_token`` is what triggered it."""

    reason: str
    offending_token: str


CommandPolicyResult = Allowed | Rejected


# Recommended presets. Operators can mix-and-match without becoming
# bash experts. None of these include ``bash``/``sh``/``python`` — add
# those explicitly when needed and accept the escape-hatch tradeoff.
MINIMAL_COMMANDS: tuple[str, ...] = ("ls", "pwd", "cat", "echo", "head", "tail")
FILE_OPS_COMMANDS: tuple[str, ...] = (
    *MINIMAL_COMMANDS,
    "touch",
    "mkdir",
    "cp",
    "mv",
    "rm",
    "ln",
    "stat",
    "wc",
)


class _Dynamic:
    """Sentinel: an executable name could not be statically resolved."""


_DYNAMIC = _Dynamic()


def check_command(
    command: str,
    allowed_commands: Sequence[str] | None,
    *,
    allow_command_substitution: bool = False,
) -> CommandPolicyResult:
    """Validate a shell command string against an allowlist.

    ``allowed_commands=None`` disables the policy entirely (back-compat).
    An empty list means no externals are allowed; only builtins.

    ``allow_command_substitution`` controls whether ``$(...)`` and backtick
    substitutions are permitted. Process substitution (``<(...)``) is
    always rejected because most consumers can't be statically reasoned
    about.
    """
    if allowed_commands is None:
        return Allowed()

    if not command.strip():
        return Allowed()

    try:
        trees = bashlex.parse(command)
    except bashlex.errors.ParsingError as error:
        return Rejected(
            reason=f"could not parse command: {error}",
            offending_token=command.strip().split()[0] if command.strip() else "",
        )
    except (NotImplementedError, IndexError, AttributeError) as error:
        # bashlex raises these for some exotic constructs; reject closed.
        return Rejected(
            reason=f"unsupported shell construct: {error}",
            offending_token="",
        )

    allowed_set = frozenset(allowed_commands)
    for tree in trees:
        result = _check_node(tree, allowed_set, allow_command_substitution)
        if isinstance(result, Rejected):
            return result
    return Allowed()


def _check_node(
    node: object,
    allowed_set: frozenset[str],
    allow_subst: bool,
) -> CommandPolicyResult:
    """Recursively validate one bashlex AST node."""
    kind = getattr(node, "kind", None)

    if kind == "command":
        executable = _extract_executable(getattr(node, "parts", ()))
        if executable is _DYNAMIC:
            return Rejected(
                reason="command name is not a static literal "
                "(uses a variable or substitution)",
                offending_token="$",
            )
        if isinstance(executable, str):
            decision = _check_executable(executable, allowed_set)
            if isinstance(decision, Rejected):
                return decision
        # Recurse into argument words to catch nested substitutions.
        for child in getattr(node, "parts", ()) or ():
            result = _check_node(child, allowed_set, allow_subst)
            if isinstance(result, Rejected):
                return result
        return Allowed()

    if kind == "commandsubstitution":
        if not allow_subst:
            return Rejected(
                reason="command substitution not allowed "
                "(set allow_command_substitution=True to enable)",
                offending_token="$(...)",
            )
        inner = getattr(node, "command", None)
        if inner is None:
            return Allowed()
        return _check_node(inner, allowed_set, allow_subst)

    if kind == "processsubstitution":
        return Rejected(
            reason="process substitution is not allowed",
            offending_token="<(...)",
        )

    if kind == "word":
        for part in getattr(node, "parts", ()) or ():
            result = _check_node(part, allowed_set, allow_subst)
            if isinstance(result, Rejected):
                return result
        return Allowed()

    # Generic recursion for compound/list/pipeline/if/for/while/function/etc.
    for attr in ("parts", "list"):
        for child in getattr(node, attr, ()) or ():
            result = _check_node(child, allowed_set, allow_subst)
            if isinstance(result, Rejected):
                return result

    inner = getattr(node, "command", None)
    if inner is not None:
        return _check_node(inner, allowed_set, allow_subst)

    return Allowed()


def _check_executable(
    name: str,
    allowed_set: frozenset[str],
) -> CommandPolicyResult:
    """Decide whether one resolved executable name is permitted."""
    if name in ALWAYS_REJECTED:
        return Rejected(
            reason=f"{name!r} is statically banned (defeats analysis)",
            offending_token=name,
        )
    if name in SHELL_BUILTINS:
        return Allowed()
    if name in allowed_set:
        return Allowed()
    return Rejected(
        reason=f"{name!r} is not in the command allowlist",
        offending_token=name,
    )


def _extract_executable(parts: Iterable[object]) -> str | _Dynamic | None:
    """Find the resolved program name from a CommandNode's child parts.

    Skips assignments, looks through ``env``/``exec``/``command`` prefixes,
    and reports ``_DYNAMIC`` if the program name embeds a parameter or
    substitution that we can't resolve statically.
    """
    words = [p for p in parts if getattr(p, "kind", None) == "word"]
    if not words:
        return None

    i = 0
    while i < len(words):
        word = words[i]
        if _is_dynamic_word(word):
            return _DYNAMIC

        token = getattr(word, "word", "")
        name = token.rsplit("/", 1)[-1] if "/" in token else token

        if name in PREFIX_COMMANDS:
            i += 1
            if name == "env":
                # Skip env's own flags and inline VAR=value assignments
                # until we find the actual program.
                while i < len(words):
                    nxt = words[i]
                    if _is_dynamic_word(nxt):
                        return _DYNAMIC
                    nxt_token = getattr(nxt, "word", "")
                    if nxt_token.startswith("-") or "=" in nxt_token:
                        i += 1
                        continue
                    break
            continue

        return name

    return None


def _is_dynamic_word(word: object) -> bool:
    """Return True if a WordNode's value can't be resolved statically."""
    return bool(getattr(word, "parts", None))
