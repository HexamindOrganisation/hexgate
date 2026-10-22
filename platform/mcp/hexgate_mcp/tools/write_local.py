"""`policy_write_local`: write a policy file on the developer's own machine.

Registered only when ``HEXGATE_MCP_LOCAL_POLICY_DIR`` is set, which it never is
in deploy, so a hosted client never sees the tool. No API call, but the
caller still needs `policy:write`.
"""

import os
import stat
import tempfile
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from hexgate_mcp.tools import caller

POLICY_SUFFIXES = {".yaml", ".yml"}
# A policy is a few KiB; past this a looping model is filling the disk.
CONTENT_CAP_BYTES = 1024 * 1024


def confine(root: Path, path: str) -> Path:
    """The real path `path` names under `root`, a resolved directory. Resolving
    first follows every `..` and symlink, so one check refuses an absolute
    path, a `..` that leaves the directory and a link out alike. The directory
    itself is refused too: it is not a file to write. The suffix and dot rules
    are name hygiene, not a sandbox: the start-up checks keep the root itself sane."""
    target = (root / path).resolve()
    if target == root or not target.is_relative_to(root):
        raise ToolError(f"{path!r} is not a file inside the local policy directory")
    parts = target.relative_to(root).parts
    if target.suffix not in POLICY_SUFFIXES or any(p.startswith(".") for p in parts):
        raise ToolError(
            f"{path!r} is not a policy file: only .yaml or .yml, outside dot-directories"
        )
    return target


def new_file_mode() -> int:
    """0644 under the process umask: owner-writable, readable by others unless
    the umask says otherwise. Read once at start-up: the umask can only be read
    by setting it."""
    umask = os.umask(0)
    os.umask(umask)
    return 0o644 & ~umask


def write_atomic(target: Path, data: bytes, new_mode: int) -> None:
    """Write through a temp file renamed over `target`, so a reader sees the
    old file or the new one, never half of it. The temp file sits in the same
    directory: a rename is atomic only within one file system. It takes the
    replaced file's permission bits, or `new_mode`, rather than mkstemp's 0600;
    never setuid, setgid, sticky or executable."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".hexgate-")
    try:
        with os.fdopen(fd, "wb") as f:
            # A directory swapped for a symlink since confine() would put the
            # file wherever the link points.
            if Path(temp).resolve().parent != target.parent:
                raise ToolError(f"{target.parent} changed while writing")
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            mode = stat.S_IMODE(target.stat().st_mode) & 0o666
        except FileNotFoundError:
            mode = new_mode
        os.chmod(temp, mode)
        os.replace(temp, target)
    except BaseException:
        os.unlink(temp)
        raise


def register(mcp: FastMCP, root: Path) -> None:
    root = root.resolve()
    new_mode = new_file_mode()

    @mcp.tool(
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
        # Unstructured, like every tool here: a `str` return otherwise goes
        # out a second time as structuredContent.
        structured_output=False,
    )
    async def policy_write_local(path: str, content: str) -> str:
        """Write a policy file into the local policy directory on this machine,
        replacing any file already there. `path` is relative to that directory,
        ends in .yaml or .yml and has no part starting with a dot; missing
        subdirectories are created. Nothing is sent to the platform."""
        # A client consented to read-only scopes must not write, here either.
        caller("policy:write")
        try:
            target = confine(root, path)
            data = content.encode()
            if len(data) > CONTENT_CAP_BYTES:
                raise ToolError(f"{path!r} is over {CONTENT_CAP_BYTES} bytes")
            # Off the event loop: the fsync would stall every other request.
            await anyio.to_thread.run_sync(write_atomic, target, data, new_mode)
        # ValueError covers a NUL in the path and UnicodeError (a lone
        # surrogate in the content).
        except (OSError, ValueError) as e:
            reason = getattr(e, "strerror", None) or e
            raise ToolError(f"could not write {path!r}: {reason}") from e
        unit = "byte" if len(data) == 1 else "bytes"
        return f"wrote {target.relative_to(root)} ({len(data)} {unit})"
