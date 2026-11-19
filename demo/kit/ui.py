"""Terminal output: short, colored, one line per step."""

from __future__ import annotations

import itertools
import sys
import threading
import time
from contextlib import contextmanager

_TTY = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def bold(s: str) -> str: return _c("1", s)
def dim(s: str) -> str: return _c("2", s)
def green(s: str) -> str: return _c("32", s)
def red(s: str) -> str: return _c("31", s)
def yellow(s: str) -> str: return _c("33", s)
def violet(s: str) -> str: return _c("38;5;141", s)


def step(s: str) -> None: print(f"{violet('›')} {s}")
def ok(s: str) -> None: print(f"  {green('✓')} {s}")
def warn(s: str) -> None: print(f"  {yellow('!')} {s}")
def fail(s: str) -> None: print(f"  {red('✗')} {s}")
def info(s: str) -> None: print(f"    {dim(s)}")


@contextmanager
def spinner(label: str):
    if not _TTY:
        print(f"    {label}…")
        yield
        return
    stop = threading.Event()

    def spin():
        for ch in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            if stop.is_set():
                break
            sys.stdout.write(f"\r    {dim(ch + ' ' + label)}   ")
            sys.stdout.flush()
            time.sleep(0.1)
        sys.stdout.write("\r" + " " * (len(label) + 12) + "\r")
        sys.stdout.flush()

    t = threading.Thread(target=spin, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()


def ask(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        a = input(f"  {question} [{hint}] ").strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


def choose(prompt: str, options: list[tuple[str, str]]) -> str | None:
    """options: (key, label). Returns the key, or None on quit."""
    for i, (_, label) in enumerate(options, 1):
        print(f"  {violet(str(i))}  {label}")
    print(f"  {dim('q  quit')}")
    while True:
        try:
            a = input(f"{prompt} ").strip().lower()
        except EOFError:
            return None
        if a in ("q", "quit", ""):
            return None
        if a.isdigit() and 1 <= int(a) <= len(options):
            return options[int(a) - 1][0]
        for key, _ in options:
            if a == key:
                return key
