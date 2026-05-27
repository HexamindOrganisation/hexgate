"""`fortify policy` subcommand — author + inspect + dry-run policy documents.

Wraps the compiler library and both enforcement engines in a five-verb
CLI: ``build``, ``validate``, ``show-rego``, ``test``, ``keygen``. Every
verb is a thin wrapper — the heavy lifting lives in
:mod:`fortify.security`. That symmetry lets the platform's save flow use
the same code without duplication.

``build`` compiles the policy to a signed WASM bundle (yaml + rego +
wasm + manifest, ``--sign-key`` to sign); ``test`` evaluates a decision
through either engine (``--engine pydantic`` by default, ``--engine
wasm`` to run the compiled module).
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
    OpaNotFoundError,
    PolicyDeniedError,
    PolicySetError,
    SignatureError,
    WasmCompileError,
    WasmEvalError,
    WasmPolicy,
    authorize_tool_call,
    compile_to_rego,
    compile_to_wasm,
    decode_key,
    encode_key,
    generate_keypair,
    load_policy_set_from_dict,
    sign_bytes,
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
        help="Compile a policy.yaml to a bundle (yaml + rego + wasm).",
        description=(
            "Compile a policy.yaml to a bundle directory. Produces the "
            "original yaml, the compiled rego, the wasm module, and a "
            "bundle.json manifest with content hashes. Skip the wasm step "
            "with --no-wasm when opa is not available."
        ),
    )
    p_build.add_argument("source", help="Path to the source policy.yaml file.")
    p_build.add_argument(
        "--out",
        default=None,
        help="Output directory (defaults to the source file's directory).",
    )
    p_build.add_argument(
        "--no-wasm",
        action="store_true",
        help="Skip the opa build -t wasm step (useful when opa is unavailable).",
    )
    p_build.add_argument(
        "--sign-key",
        default=None,
        metavar="PATH",
        help=(
            "Path to an Ed25519 private key (base64url, from `fortify policy "
            "keygen`). When set, signs the bundle manifest and writes a "
            "detached {stem}.bundle.json.sig. Production bundles come signed "
            "by the platform; this flag is for local/CI signing."
        ),
    )
    p_build.set_defaults(func=_main_build)

    # ---- keygen ----
    p_keygen = sub.add_parser(
        "keygen",
        help="Generate an Ed25519 keypair for signing bundles locally.",
        description=(
            "Write a fresh Ed25519 keypair (raw keys, base64url-encoded) to "
            "disk: <out>.private for signing (`build --sign-key`) and "
            "<out>.public for verifying (FORTIFY_BUNDLE_PUBKEY_PATH). For "
            "local/CI use — production signing keys live in the platform "
            "keystore."
        ),
    )
    p_keygen.add_argument(
        "--out",
        required=True,
        metavar="PREFIX",
        help="Output path prefix; writes PREFIX.private + PREFIX.public.",
    )
    p_keygen.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing key files at the prefix.",
    )
    p_keygen.set_defaults(func=_main_keygen)

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
    p_test.add_argument(
        "--engine",
        choices=("pydantic", "wasm"),
        default="pydantic",
        help=(
            "Decision engine: pydantic (default — fast, no opa needed) or "
            "wasm (compiles the policy via opa and evaluates in wasmtime; "
            "matches what production will run)."
        ),
    )
    p_test.set_defaults(func=_main_test)


def main(args: argparse.Namespace) -> int:
    """Entry point used by the top-level dispatcher in fortify/cli/__init__.py."""
    return args.func(args)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _main_keygen(args: argparse.Namespace) -> int:
    """Generate an Ed25519 keypair and write it base64url-encoded to disk."""
    prefix = Path(args.out)
    private_out = prefix.with_name(prefix.name + ".private")
    public_out = prefix.with_name(prefix.name + ".public")

    if not args.force:
        for existing in (private_out, public_out):
            if existing.exists():
                print(
                    f"{existing} already exists — pass --force to overwrite.",
                    file=sys.stderr,
                )
                return 1

    parent = prefix.parent
    if str(parent):
        parent.mkdir(parents=True, exist_ok=True)

    private_raw, public_raw = generate_keypair()
    # 0600 the private key — it's a signing secret.
    private_out.write_text(encode_key(private_raw) + "\n", encoding="utf-8")
    private_out.chmod(0o600)
    public_out.write_text(encode_key(public_raw) + "\n", encoding="utf-8")

    print(f"✓ Wrote {private_out} (private signing key — keep secret, .gitignore it)")
    print(f"✓ Wrote {public_out} (public verify key)")
    print(
        "\nSign a bundle:   fortify policy build <policy.yaml> "
        f"--sign-key {private_out}"
    )
    print(
        f"Verify at runtime:  export FORTIFY_BUNDLE_PUBKEY_PATH={public_out}"
    )
    return 0


def _main_build(args: argparse.Namespace) -> int:
    """Compile + write the bundle artifacts (yaml + rego + wasm [+ signature])."""
    source_path = Path(args.source)
    source_text, payload, err = _read_and_parse(source_path)
    if err is not None:
        print(err, file=sys.stderr)
        return 1

    # Load the signing key early so a bad key fails before we write anything.
    sign_key: bytes | None = None
    if getattr(args, "sign_key", None):
        sign_key, err = _read_signing_key(Path(args.sign_key))
        if err is not None:
            print(err, file=sys.stderr)
            return 1

    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    try:
        rego = compile_to_rego(payload, source_hash=source_hash)
    except (PolicySetError, ConstraintParseError, ValidationError) as exc:
        print(f"compile error: {exc}", file=sys.stderr)
        return 1

    wasm_bytes: bytes | None = None
    if not args.no_wasm:
        try:
            wasm_bytes = compile_to_wasm(rego).wasm
        except OpaNotFoundError as exc:
            print(
                f"wasm compile skipped — {exc}\n"
                "Pass --no-wasm to suppress this and emit yaml+rego only.",
                file=sys.stderr,
            )
            return 1
        except WasmCompileError as exc:
            print(f"wasm compile error: {exc}", file=sys.stderr)
            return 1

    # Resolve to an absolute path up front so all derived paths are
    # unambiguous — a relative --out otherwise breaks the relative_to()
    # display math below.
    out_dir = (Path(args.out) if args.out else source_path.parent).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = source_path.stem  # "billing.yaml" → "billing"
    yaml_out = out_dir / f"{stem}.yaml"
    rego_out = out_dir / f"{stem}.rego"
    wasm_out = out_dir / f"{stem}.wasm"
    bundle_out = out_dir / f"{stem}.bundle.json"

    # Always rewrite the trio so the bundle stays consistent — if the dev
    # is reusing the source dir as the output dir, the YAML write is a
    # no-op (same bytes).
    yaml_out.write_text(source_text, encoding="utf-8")
    rego_out.write_text(rego, encoding="utf-8")
    rego_hash = hashlib.sha256(rego.encode("utf-8")).hexdigest()
    wasm_hash: str | None = None
    if wasm_bytes is not None:
        wasm_out.write_bytes(wasm_bytes)
        wasm_hash = hashlib.sha256(wasm_bytes).hexdigest()
    manifest = {
        "version": 1,
        "source": str(source_path.name),
        "source_hash": source_hash,
        "rego_hash": rego_hash,
        "wasm_hash": wasm_hash,
    }
    # Capture the exact bytes we write — the signature is computed over
    # these, so there's no JSON-canonicalization gap at verify time.
    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    bundle_out.write_bytes(manifest_bytes)

    sig_out = out_dir / f"{stem}.bundle.json.sig"
    if sign_key is not None:
        sig_out.write_bytes(sign_bytes(manifest_bytes, sign_key))

    print(f"✓ Wrote {_display_path(yaml_out)}")
    print(f"✓ Wrote {_display_path(rego_out)}")
    if wasm_bytes is not None:
        print(f"✓ Wrote {_display_path(wasm_out)}")
    else:
        print("ⓘ wasm step skipped (--no-wasm)", file=sys.stderr)
    print(f"✓ Wrote {_display_path(bundle_out)}")
    if sign_key is not None:
        print(f"✓ Wrote {_display_path(sig_out)} (signed)")
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
    """Dry-run a single (role, tool, args) decision through the chosen engine."""
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

    label = f'{args.role} → {args.tool}({json.dumps(tool_args, sort_keys=True)})'
    engine = getattr(args, "engine", "pydantic")

    if engine == "wasm":
        return _test_via_wasm(payload, args.role, args.tool, tool_args, label)
    return _test_via_pydantic(policy_set, args.role, args.tool, tool_args, label)


def _test_via_pydantic(
    policy_set: Any, role: str, tool: str, tool_args: dict, label: str
) -> int:
    """Run the decision through the in-process constraint evaluator."""
    policy: AgentPolicy = policy_set.policy_for(role)
    try:
        authorize_tool_call(policy, tool, tool_args)
    except ApprovalRequiredError as exc:
        print(f"⚠ APPROVAL_REQUIRED · {label}\n  reason: {exc}")
        return 0
    except PolicyDeniedError as exc:
        print(f"✗ DENY · {label}\n  reason: {exc}")
        return 1
    print(f"✓ ALLOW · {label}")
    return 0


def _test_via_wasm(
    payload: dict, role: str, tool: str, tool_args: dict, label: str
) -> int:
    """Compile to wasm on the fly + evaluate — matches production semantics."""
    try:
        rego = compile_to_rego(payload)
    except (PolicySetError, ConstraintParseError, ValidationError) as exc:
        print(f"compile error: {exc}", file=sys.stderr)
        return 1
    try:
        artifact = compile_to_wasm(rego)
    except (OpaNotFoundError, WasmCompileError) as exc:
        print(f"wasm compile error: {exc}", file=sys.stderr)
        return 1
    try:
        wasm_policy = WasmPolicy.from_bytes(artifact.wasm)
        decision = wasm_policy.decide(role=role, tool=tool, args=tool_args)
    except WasmEvalError as exc:
        print(f"wasm eval error: {exc}", file=sys.stderr)
        return 1

    if decision.allow:
        print(f"✓ ALLOW · {label}")
        return 0
    if decision.requires_approval:
        print(f"⚠ APPROVAL_REQUIRED · {label}")
        return 0
    print(f"✗ DENY · {label}")
    if decision.violations:
        print("  violations:")
        for v in decision.violations:
            print(f"    • {v}")
    return 1


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


def _read_signing_key(key_path: Path) -> tuple[bytes | None, str | None]:
    """Read + decode a base64url Ed25519 private key. Returns (key, error?)."""
    if not key_path.is_file():
        return None, f"--sign-key: no such file: {key_path}"
    try:
        encoded = key_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"--sign-key: cannot read {key_path}: {exc}"
    try:
        raw = decode_key(encoded)
    except SignatureError as exc:
        return None, f"--sign-key: {key_path} is not a valid base64url key: {exc}"
    if len(raw) != 32:
        return None, (
            f"--sign-key: {key_path} decodes to {len(raw)} bytes, expected 32 "
            "(raw Ed25519 private key from `fortify policy keygen`)."
        )
    return raw, None


def _display_path(path: Path) -> str:
    """Render a path relative to cwd when it's underneath, else absolute.

    Resolves both sides so the comparison and the rendered string stay
    consistent — a relative ``--out`` would otherwise pass an "under cwd"
    check but blow up on ``relative_to`` against an absolute cwd.
    """
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(resolved)
