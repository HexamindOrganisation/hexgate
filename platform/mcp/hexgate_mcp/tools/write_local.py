"""`policy_write_local`: write a policy file on the developer's own machine.

Registered only when ``HEXGATE_MCP_LOCAL_POLICY_DIR`` is set, which it never is
in deploy, so a hosted client never sees the tool. No API call, so no scope.
"""

import os
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations


def confine(root: Path, path: str) -> Path:
    """The real path `path` names under `root`, a resolved directory. Resolving
    first follows every `..` and symlink, so one check refuses an absolute
    path, a `..` anywhere in it and a link out alike. The directory itself is
    refused too: it is not a file to write."""
    target = (root / path).resolve()
    if target == root or not target.is_relative_to(root):
        raise ToolError(f"{path!r} is not a file inside the local policy directory")
    return target


def write_atomic(target: Path, content: str) -> None:
    """Write through a temp file renamed over `target`, so a reader sees the
    old file or the new one, never half of it. The temp file sits in the same
    directory: a rename is atomic only within one file system."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, target)
    except BaseException:
        os.unlink(temp)
        raise


def register(mcp: FastMCP, root: Path) -> None:
    root = root.resolve()

    @mcp.tool(
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
        # Unstructured, like every tool here: a `str` return otherwise goes
        # out a second time as structuredContent.
        structured_output=False,
    )
    def policy_write_local(path: str, content: str) -> str:
        """Write a policy file into the local policy directory on this machine,
        replacing any file already there. `path` is relative to that directory;
        missing subdirectories are created. Nothing is sent to the platform."""
        target = confine(root, path)
        try:
            write_atomic(target, content)
        except OSError as e:
            raise ToolError(f"could not write {path!r}: {e.strerror}") from e
        return f"wrote {target.relative_to(root)} ({len(content.encode())} bytes)"
