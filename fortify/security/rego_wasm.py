"""Compile Rego source to a WASM bundle via the ``opa`` binary.

This is the second half of the M2 compile pipeline: Phase 1 emits Rego
text (``fortify.security.rego``), Phase 3 turns that text into a
self-contained ``policy.wasm`` blob the runtime can evaluate without
embedding an interpreter.

The implementation shells out to ``opa build -t wasm`` rather than
linking against an opa library — opa is the canonical reference
implementation and pinning to a release line gives us a clean spec to
test against. Discovery checks ``$FORTIFY_OPA_BIN`` first, then falls
back to ``opa`` on ``PATH``; both raise a friendly install hint if missing.

The emitted Rego exposes a single ``decision`` entrypoint (returning
``{allow, requires_approval, violations}``); we compile it into one
bundle so the runtime loads a single module. The returned
``WasmArtifact`` carries the raw ``.wasm`` bytes plus the manifest opa
writes alongside it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Pin a floor we have actually tested against. opa is stable about
# breakage but we don't want to silently absorb a future Rego dialect
# bump from a much older opa.
_OPA_MIN_VERSION = (0, 60, 0)

# Single structured-decision entrypoint — kept in sync with rego.py.
# The bundled wasm exposes one query that returns the full verdict object
# ({allow, requires_approval, violations}) so the runtime only needs one
# eval call per tool-call decision.
DEFAULT_ENTRYPOINTS: tuple[str, ...] = ("fortify/policy/decision",)


class WasmCompileError(RuntimeError):
    """``opa build`` returned a non-zero status or produced no module."""


class OpaNotFoundError(RuntimeError):
    """opa is not on PATH and ``$FORTIFY_OPA_BIN`` is unset or invalid."""


@dataclass(frozen=True)
class WasmArtifact:
    """A compiled OPA bundle.

    ``wasm`` is the raw module bytes (what the wasmtime adapter loads).
    ``manifest`` is the bundle's ``.manifest`` JSON — the runtime uses
    ``manifest["wasm"]`` to map entrypoint names to module paths.
    """

    wasm: bytes
    manifest: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compile_to_wasm(
    rego_text: str,
    *,
    entrypoints: tuple[str, ...] = DEFAULT_ENTRYPOINTS,
    opa_bin: str | None = None,
) -> WasmArtifact:
    """Compile Rego source to a WASM bundle.

    Writes ``rego_text`` to a tempdir, runs ``opa build -t wasm`` with
    one ``-e`` flag per entrypoint, untars the result, and returns the
    bytes of ``policy.wasm`` + the bundle manifest.

    Raises ``OpaNotFoundError`` if opa can't be found and
    ``WasmCompileError`` for any compile failure (the opa stderr is
    surfaced so policy authors see the real diagnostic).
    """
    opa = opa_bin or _discover_opa()
    _check_opa_version(opa)

    if not entrypoints:
        raise ValueError("compile_to_wasm requires at least one entrypoint")

    with tempfile.TemporaryDirectory(prefix="fortify-opa-build-") as tmp:
        tmpdir = Path(tmp)
        rego_path = tmpdir / "policy.rego"
        rego_path.write_text(rego_text, encoding="utf-8")
        bundle_path = tmpdir / "bundle.tar.gz"

        # Invoke opa with relative paths from the tempdir so the embedded
        # source path is just "policy.rego" — that makes wasm bytes
        # deterministic across builds of the same input.
        cmd = [opa, "build", "-t", "wasm"]
        for entry in entrypoints:
            cmd += ["-e", entry]
        cmd += ["-o", "bundle.tar.gz", "policy.rego"]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=tmpdir,
        )
        if proc.returncode != 0:
            raise WasmCompileError(
                f"opa build failed (exit {proc.returncode}):\n"
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        if not bundle_path.is_file():
            raise WasmCompileError(
                "opa build reported success but produced no bundle; "
                f"stderr: {proc.stderr.strip()!r}"
            )

        return _extract_artifact(bundle_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _discover_opa() -> str:
    """Return a usable path to the opa binary or raise OpaNotFoundError."""
    env = os.environ.get("FORTIFY_OPA_BIN")
    if env:
        if not Path(env).is_file():
            raise OpaNotFoundError(
                f"FORTIFY_OPA_BIN points to {env!r} but that file does not exist."
            )
        return env
    found = shutil.which("opa")
    if found:
        return found
    raise OpaNotFoundError(
        "opa not found on PATH. Install it from "
        "https://www.openpolicyagent.org/docs/latest/#running-opa, "
        "or set FORTIFY_OPA_BIN to the binary path. "
        "On macOS: `brew install opa`."
    )


def _check_opa_version(opa: str) -> None:
    """Best-effort: refuse opa older than our tested floor."""
    try:
        proc = subprocess.run(
            [opa, "version"], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise OpaNotFoundError(f"could not execute {opa!r}: {exc}") from exc
    parsed = _parse_opa_version(proc.stdout)
    if parsed is not None and parsed < _OPA_MIN_VERSION:
        floor = ".".join(str(p) for p in _OPA_MIN_VERSION)
        current = ".".join(str(p) for p in parsed)
        raise WasmCompileError(
            f"opa {current} is below the tested floor (>= {floor}); please upgrade."
        )


def _parse_opa_version(stdout: str) -> tuple[int, int, int] | None:
    """Parse the ``Version:`` line from `opa version` output."""
    for line in stdout.splitlines():
        if not line.startswith("Version:"):
            continue
        raw = line.split(":", 1)[1].strip().lstrip("v")
        parts = raw.split(".")
        if len(parts) < 3:
            return None
        try:
            return (int(parts[0]), int(parts[1]), int(parts[2].split("-")[0]))
        except ValueError:
            return None
    return None


def _extract_artifact(bundle_path: Path) -> WasmArtifact:
    """Pull policy.wasm + .manifest out of the bundle tarball."""
    wasm_bytes: bytes | None = None
    manifest: dict = {}
    with tarfile.open(bundle_path, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name.lstrip("/")
            if name == "policy.wasm":
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                wasm_bytes = fh.read()
            elif name == ".manifest":
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                try:
                    manifest = json.loads(fh.read().decode("utf-8"))
                except json.JSONDecodeError:
                    manifest = {}
    if wasm_bytes is None:
        raise WasmCompileError(
            "opa build produced a bundle but no policy.wasm inside it."
        )
    if not wasm_bytes.startswith(b"\x00asm"):
        raise WasmCompileError(
            "policy.wasm is missing the WebAssembly magic header — "
            "the bundle is corrupt."
        )
    return WasmArtifact(wasm=wasm_bytes, manifest=manifest)
