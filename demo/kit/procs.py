"""Background processes: start with a log file, remember the pid, stop the whole tree."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from kit import ui
from kit.paths import RUN

PIDS = RUN / "pids"


def _read() -> dict[str, int]:
    if not PIDS.exists():
        return {}
    out = {}
    for line in PIDS.read_text().splitlines():
        name, _, pid = line.partition(" ")
        if pid.isdigit():
            out[name] = int(pid)
    return out


def _write(pids: dict[str, int]) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    PIDS.write_text("".join(f"{n} {p}\n" for n, p in pids.items()))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def running(name: str) -> int | None:
    pid = _read().get(name)
    return pid if pid and alive(pid) else None


def start(name: str, cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> int:
    """Start `cmd` detached (its own session, so it outlives this launcher)."""
    RUN.mkdir(parents=True, exist_ok=True)
    log = open(RUN / f"{name}.log", "ab")
    log.write(f"\n==== {time.strftime('%H:%M:%S')} {' '.join(cmd)}\n".encode())
    log.flush()
    p = subprocess.Popen(
        cmd, cwd=cwd, env={**os.environ, **(env or {})},
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    pids = _read()
    pids[name] = p.pid
    _write(pids)
    return p.pid


def _children(pid: int) -> list[int]:
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return []
    return [int(x) for x in out.split()]


def kill_tree(pid: int) -> None:
    for child in _children(pid):
        kill_tree(child)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def stop(name: str) -> bool:
    pids = _read()
    pid = pids.pop(name, None)
    _write(pids)
    if pid and alive(pid):
        kill_tree(pid)
        for _ in range(30):
            if not alive(pid):
                break
            time.sleep(0.1)
        return True
    return False


def names() -> list[str]:
    return list(_read())


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def port_owner(port: int) -> str:
    """'pid command' of whoever listens on the port, or ''."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"], capture_output=True, text=True
        ).stdout
    except FileNotFoundError:
        return ""
    pid = cmd = ""
    for line in out.splitlines():
        if line.startswith("p") and not pid:
            pid = line[1:]
        elif line.startswith("c") and not cmd:
            cmd = line[1:]
    return f"{pid} {cmd}".strip()


def http_ok(url: str, timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 500
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        return code is not None and code < 500


def wait_http(name: str, url: str, seconds: int = 90) -> bool:
    with ui.spinner(f"waiting for {name}"):
        for _ in range(seconds * 2):
            if http_ok(url):
                return True
            pid = running(name)
            if pid is None and name in names():
                break  # it died
            time.sleep(0.5)
    ui.fail(f"{name} did not come up. Last lines of {RUN / (name + '.log')}:")
    log = RUN / f"{name}.log"
    if log.exists():
        for line in log.read_text(errors="replace").splitlines()[-15:]:
            print("     " + line)
    return False
