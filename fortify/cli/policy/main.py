"""`fortify policy` subcommand — author + inspect + dry-run policy documents.

Wraps the M2 phase 1 compiler library and the existing pydantic engine in
a four-verb CLI: ``build``, ``validate``, ``show-rego``, ``test``. Every
verb is a thin wrapper — the heavy lifting lives in
:mod:`fortify.security`. That symmetry lets the platform's save flow use
the same code without duplication.

Phase 3 will extend ``build`` with the ``opa build -t wasm`` step;
Phase 4 will extend ``test`` to evaluate against the compiled WASM
directly (today it uses the pydantic engine — same decisions but a
different evaluator path).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.error import MarkedYAMLError

from fortify.security import (
    AgentPolicy,
    ApprovalRequiredError,
    PolicyDeniedError,
    PolicySetError,
    authorize_tool_call,
    compile_to_rego,
    load_policy_set_from_dict,
)
from fortify.security.constraints import ConstraintParseError, parse_constraint


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``policy`` subcommand on the top-level fortify CLI."""
    parser = subparsers.add_parser(
        "policy",
        help="Author, inspect, and dry-run agent policy documents.",
        description="Author, inspect, and dry-run agent policy documents.",
    )
    sub = parser.add_subparsers(
        dest="policy_cmd", required=True, metavar="subcommand"
    )

    # ---- build ----
    p_build = sub.add_parser(
        "build",
        help="Compile a policy.yaml to a bundle (yaml + rego today; wasm in M2 phase 3).",
        description=(
            "Compile a policy.yaml to a bundle directory. Today produces the "
            "original yaml + the compiled rego next to it; the .wasm artifact "
            "lands when phase 3 adds the opa build step."
        ),
    )
    p_build.add_argument("source", help="Path to the source policy.yaml file.")
    p_build.add_argument(
        "--out",
        default=None,
        help="Output directory (defaults to the source file's directory).",
    )
    p_build.set_defaults(func=_main_build)

    # ---- validate ----
    p_val = sub.add_parser(
        "validate",
        help="Parse the YAML and check every constraint against the grammar.",
        description=(
            "Runs the same checks the platform's /validate endpoint does, "
            "but locally — no network needed. Exits 0 on success, 1 on any "
            "error (with all errors printed)."
        ),
    )
    p_val.add_argument("source", help="Path to the policy.yaml file.")
    p_val.set_defaults(func=_main_validate)

    # ---- show-rego ----
    p_show = sub.add_parser(
        "show-rego",
        help="Compile the policy and print the resulting Rego to stdout.",
        description=(
            "Useful for spotting what Rego rules your YAML produces, before "
            "you trust them in production. Output goes to stdout so you can "
            "pipe it to a file or opa eval."
        ),
    )
    p_show.add_argument("source", help="Path to the policy.yaml file.")
    p_show.set_defaults(func=_main_show_rego)

    # ---- test ----
    p_test = sub.add_parser(
        "test",
        help="Dry-run a tool-call decision against the policy.",
        description=(
            "Runs authorize_tool_call against the given role/tool/args without "
            "spinning up the agent. Prints ALLOW / DENY / APPROVAL_REQUIRED "
            "with the offending constraint when relevant. Designed for "
            "CI policy-test suites."
        ),
    )
    p_test.add_argument("source", help="Path to the policy.yaml file.")
    p_test.add_argument(
        "--role",
        required=True,
        help='Role to evaluate as, e.g. "billing".',
    )
    p_test.add_argument(
        "--tool",
        required=True,
        help='Tool the agent is calling, e.g. "refund_order".',
    )
    p_test.add_argument(
        "--args",
        default="{}",
        help='Tool arguments as a JSON object (e.g. \'{"amount": 30, "currency": "USD"}\'). Defaults to {}.',
    )
    p_test.set_defaults(func=_main_test)

    parser.set_defaults(func=_main_unknown)


def main(args: argparse.Namespace) -> int:
    """Entry point used by the top-level dispatcher in fortify/cli/__init__.py."""
    return args.func(args)


def _main_unknown(args: argparse.Namespace) -> int:
    """argparse fallback for ``fortify policy`` with no subcommand."""
    print(
        "fortify policy: choose a subcommand (build / validate / show-rego / test). "
        "See `fortify policy --help`.",
        file=sys.stderr,
    )
    return 2


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _main_build(args: argparse.Namespace) -> int:
    """Compile + write the bundle artifacts (yaml + rego)."""
    source_path = Path(args.source)
    source_text, payload, err = _read_and_parse(source_path)
    if err is not None:
        print(err, file=sys.stderr)
        return 1

    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    try:
        rego = compile_to_rego(payload, source_hash=source_hash)
    except (PolicySetError, ConstraintParseError, ValidationError) as exc:
        print(f"compile error: {exc}", file=sys.stderr)
        return 1

    out_dir = Path(args.out) if args.out else source_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = source_path.stem  # "billing.yaml" → "billing"
    yaml_out = out_dir / f"{stem}.yaml"
    rego_out = out_dir / f"{stem}.rego"
    bundle_out = out_dir / f"{stem}.bundle.json"

    # Always rewrite all three so the bundle stays consistent — if the dev
    # is reusing the source dir as the output dir, the YAML write is a
    # no-op (same bytes).
    yaml_out.write_text(source_text, encoding="utf-8")
    rego_out.write_text(rego, encoding="utf-8")
    rego_hash = hashlib.sha256(rego.encode("utf-8")).hexdigest()
    manifest = {
        "version": 1,
        "source": str(source_path.name),
        "source_hash": source_hash,
        "rego_hash": rego_hash,
        # WASM hash lands in phase 3 when opa build wires in.
        "wasm_hash": None,
    }
    bundle_out.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(f"✓ Wrote {yaml_out.relative_to(Path.cwd()) if _is_under_cwd(yaml_out) else yaml_out}")
    print(f"✓ Wrote {rego_out.relative_to(Path.cwd()) if _is_under_cwd(rego_out) else rego_out}")
    print(f"✓ Wrote {bundle_out.relative_to(Path.cwd()) if _is_under_cwd(bundle_out) else bundle_out}")
    print(
        "ⓘ .wasm output not yet implemented — coming in phase 3 with the "
        "`opa build` step.",
        file=sys.stderr,
    )
    return 0


def _main_validate(args: argparse.Namespace) -> int:
    """Mirror the platform's /validate endpoint, locally."""
    source_path = Path(args.source)
    source_text, payload, err = _read_and_parse(source_path)
    if err is not None:
        print(err, file=sys.stderr)
        return 1

    errors: list[str] = []

    # Walk the policy set so inheritance + mixin filtering apply.
    try:
        policy_set = load_policy_set_from_dict(payload)
    except (PolicySetError, ValidationError) as exc:
        print(f"policy schema: {exc}", file=sys.stderr)
        return 1

    # Constraint grammar check across every (role, tool) pair the runtime
    # would see at decision time.
    for role in policy_set.roles:
        policy = policy_set.policy_for(role)
        for tool_name, tool_policy in policy.tools.items():
            for raw in tool_policy.constraints:
                try:
                    parse_constraint(raw)
                except ConstraintParseError as exc:
                    errors.append(f"{role} → {tool_name}: {exc}")

    if errors:
        print(f"{len(errors)} constraint error(s):", file=sys.stderr)
        for e in errors:
            print(f"  • {e}", file=sys.stderr)
        return 1

    print("✓ Policy parses cleanly.")
    return 0


def _main_show_rego(args: argparse.Namespace) -> int:
    """Compile + print to stdout. No file writes."""
    source_path = Path(args.source)
    source_text, payload, err = _read_and_parse(source_path)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    try:
        rego = compile_to_rego(
            payload,
            source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        )
    except (PolicySetError, ConstraintParseError, ValidationError) as exc:
        print(f"compile error: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(rego)
    return 0


def _main_test(args: argparse.Namespace) -> int:
    """Dry-run a single (role, tool, args) decision through the engine."""
    source_path = Path(args.source)
    source_text, payload, err = _read_and_parse(source_path)
    if err is not None:
        print(err, file=sys.stderr)
        return 1

    try:
        tool_args: dict[str, Any] = json.loads(args.args)
    except json.JSONDecodeError as exc:
        print(f"--args is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(tool_args, dict):
        print("--args must be a JSON object (dict).", file=sys.stderr)
        return 1

    try:
        policy_set = load_policy_set_from_dict(payload)
    except (PolicySetError, ValidationError) as exc:
        print(f"policy schema: {exc}", file=sys.stderr)
        return 1

    if args.role not in policy_set:
        print(
            f'role "{args.role}" not in policy '
            f"(known roles: {policy_set.roles!r})",
            file=sys.stderr,
        )
        return 1

    policy: AgentPolicy = policy_set.policy_for(args.role)

    label = f'{args.role} → {args.tool}({json.dumps(tool_args, sort_keys=True)})'
    try:
        authorize_tool_call(policy, args.tool, tool_args)
    except ApprovalRequiredError as exc:
        print(f"⚠ APPROVAL_REQUIRED · {label}\n  reason: {exc}")
        return 0
    except PolicyDeniedError as exc:
        print(f"✗ DENY · {label}\n  reason: {exc}")
        return 1
    print(f"✓ ALLOW · {label}")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_and_parse(
    source_path: Path,
) -> tuple[str, dict[str, Any], str | None]:
    """Load + parse a policy.yaml. Returns (text, parsed, error_message?)."""
    if not source_path.is_file():
        return "", {}, f"no such file: {source_path}"
    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        return "", {}, f"cannot read {source_path}: {exc}"
    try:
        parsed = yaml.safe_load(text) or {}
    except MarkedYAMLError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark else None
        loc = f" (line {line})" if line is not None else ""
        return text, {}, f"YAML parse error{loc}: {exc.problem or exc}"
    if not isinstance(parsed, dict):
        return text, {}, f"{source_path} must contain a YAML mapping at top level"
    return text, parsed, None


def _is_under_cwd(path: Path) -> bool:
    """Best-effort check used only to prettify path output."""
    try:
        path.resolve().relative_to(Path.cwd().resolve())
        return True
    except ValueError:
        return False
