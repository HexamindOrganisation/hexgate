"""Tests for the ``fortify policy`` CLI subcommand (M2 phase 2).

Each subcommand has its own ``main`` function that takes a parsed
``argparse.Namespace`` and returns an exit code. Tests build the
namespace directly (skipping the top-level parser) and inspect exit
codes + stdout/stderr capture.

A small in-memory YAML fixture exercises the role-aware shape from
the support_bot demo so the parity tests stay aligned with what the
runtime sees.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from fortify.cli.policy.main import (
    _main_build,
    _main_show_rego,
    _main_test,
    _main_validate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_DEMO_POLICY = """\
version: 1
roles:
  read_only:
    is_mixin: true
    tools:
      web_search: { mode: allow }
  default:
    inherits: [read_only]
    tools:
      refund_order: { mode: deny }
  billing:
    inherits: [read_only]
    tools:
      refund_order:
        mode: allow
        constraints:
          - args.amount <= 500
          - args.currency in ["USD", "EUR"]
"""


@pytest.fixture
def policy_file(tmp_path: Path) -> Path:
    """A scratch policy.yaml with the support_bot demo shape."""
    p = tmp_path / "billing.yaml"
    p.write_text(_DEMO_POLICY, encoding="utf-8")
    return p


def _ns(**kwargs) -> argparse.Namespace:
    """Convenience for building an argparse.Namespace with sane defaults."""
    return argparse.Namespace(**kwargs)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_passes_on_clean_policy(
    policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A well-formed policy.yaml exits 0 with a success line on stdout."""
    rc = _main_validate(_ns(source=str(policy_file)))
    out = capsys.readouterr()
    assert rc == 0
    assert "parses cleanly" in out.out


def test_validate_reports_constraint_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unsupported operator surfaces as a constraint error with role+tool."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: 1\n"
        "roles:\n"
        "  default:\n"
        "    tools:\n"
        "      refund:\n"
        "        mode: allow\n"
        "        constraints:\n"
        "          - args.amount ~~ 50\n",
        encoding="utf-8",
    )
    rc = _main_validate(_ns(source=str(bad)))
    err = capsys.readouterr().err
    assert rc == 1
    assert "default → refund" in err
    assert "no recognised operator" in err


def test_validate_handles_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing source path exits 1 with a clear error message."""
    rc = _main_validate(_ns(source=str(tmp_path / "nope.yaml")))
    err = capsys.readouterr().err
    assert rc == 1
    assert "no such file" in err


def test_validate_handles_malformed_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A YAML lex error surfaces with the offending line number."""
    p = tmp_path / "broken.yaml"
    p.write_text("tools: [bad: unclosed\n", encoding="utf-8")
    rc = _main_validate(_ns(source=str(p)))
    err = capsys.readouterr().err
    assert rc == 1
    assert "YAML parse error" in err
    assert "line" in err.lower()


# ---------------------------------------------------------------------------
# show-rego
# ---------------------------------------------------------------------------


def test_show_rego_emits_module_to_stdout(
    policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The compiled Rego goes to stdout, exit 0."""
    rc = _main_show_rego(_ns(source=str(policy_file)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "package fortify.policy" in out
    assert "default allow := false" in out
    # Demo's billing rule should be visible
    assert 'input.role == "billing"' in out
    assert "input.args.amount <= 500" in out


def test_show_rego_filters_mixin(
    policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mixin roles never surface as `input.role == "<mixin>"`."""
    _main_show_rego(_ns(source=str(policy_file)))
    out = capsys.readouterr().out
    assert 'input.role == "read_only"' not in out


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def test_build_writes_bundle_files(
    policy_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """build produces {stem}.yaml + {stem}.rego + {stem}.bundle.json."""
    out_dir = tmp_path / "build"
    rc = _main_build(
        _ns(source=str(policy_file), out=str(out_dir))
    )
    capsys.readouterr()  # drain
    assert rc == 0
    assert (out_dir / "billing.yaml").exists()
    assert (out_dir / "billing.rego").exists()
    assert (out_dir / "billing.bundle.json").exists()


def test_build_bundle_manifest_carries_hashes(
    policy_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bundle.json records source + rego hashes; wasm_hash is None today."""
    out_dir = tmp_path / "build"
    _main_build(_ns(source=str(policy_file), out=str(out_dir)))
    capsys.readouterr()
    bundle = json.loads((out_dir / "billing.bundle.json").read_text())
    assert bundle["version"] == 1
    assert bundle["source"] == "billing.yaml"
    assert len(bundle["source_hash"]) == 64
    assert len(bundle["rego_hash"]) == 64
    assert bundle["wasm_hash"] is None  # phase 3 fills this in


def test_build_defaults_output_to_source_dir(
    policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --out, artifacts land next to the source file."""
    rc = _main_build(_ns(source=str(policy_file), out=None))
    capsys.readouterr()
    assert rc == 0
    assert (policy_file.parent / "billing.rego").exists()
    assert (policy_file.parent / "billing.bundle.json").exists()


def test_build_rejects_unparseable_constraint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bad constraint → compile error → exit 1, no files written."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: 1\nroles:\n  default:\n    tools:\n      r:\n        mode: allow\n"
        "        constraints: [args.amount ~~ 50]\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "build"
    rc = _main_build(_ns(source=str(bad), out=str(out_dir)))
    err = capsys.readouterr().err
    assert rc == 1
    assert "compile error" in err
    # Nothing should have been written into the output dir.
    assert not out_dir.exists() or not list(out_dir.iterdir())


# ---------------------------------------------------------------------------
# test (dry-run)
# ---------------------------------------------------------------------------


def test_test_allows_when_constraints_pass(
    policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """billing/refund_order with amount=200 USD → ALLOW (exit 0)."""
    rc = _main_test(
        _ns(
            source=str(policy_file),
            role="billing",
            tool="refund_order",
            args='{"amount": 200, "currency": "USD"}',
        )
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "ALLOW" in out


def test_test_denies_when_constraint_fails(
    policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """billing/refund_order with amount=700 → DENY, exit 1, reason surfaced."""
    rc = _main_test(
        _ns(
            source=str(policy_file),
            role="billing",
            tool="refund_order",
            args='{"amount": 700, "currency": "USD"}',
        )
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "DENY" in out
    assert "args.amount <= 500" in out


def test_test_denies_when_mode_is_deny(
    policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """default/refund_order is mode: deny — exits 1 regardless of args."""
    rc = _main_test(
        _ns(
            source=str(policy_file),
            role="default",
            tool="refund_order",
            args="{}",
        )
    )
    assert rc == 1
    assert "DENY" in capsys.readouterr().out


def test_test_rejects_unknown_role(
    policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown role exits 1 with the list of known roles."""
    rc = _main_test(
        _ns(
            source=str(policy_file),
            role="nope",
            tool="refund_order",
            args="{}",
        )
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert 'role "nope"' in err
    assert "billing" in err


def test_test_rejects_invalid_args_json(
    policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--args must be valid JSON; clear error when it isn't."""
    rc = _main_test(
        _ns(
            source=str(policy_file),
            role="billing",
            tool="refund_order",
            args="{not json",
        )
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "not valid JSON" in err


def test_test_rejects_non_object_args(
    policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--args must be a JSON object — lists and scalars rejected."""
    rc = _main_test(
        _ns(
            source=str(policy_file),
            role="billing",
            tool="refund_order",
            args="[1, 2, 3]",
        )
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "JSON object" in err


# ---------------------------------------------------------------------------
# Wiring — confirm fortify policy reaches our subcommand handlers
# ---------------------------------------------------------------------------


def test_top_level_dispatch_routes_to_policy(
    policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The top-level `fortify` parser routes `policy validate <file>` here."""
    from fortify.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["policy", "validate", str(policy_file)])
    rc = args.func(args)
    assert rc == 0
    assert "parses cleanly" in capsys.readouterr().out


def test_top_level_dispatch_routes_to_show_rego(
    policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`fortify policy show-rego` routes through and emits Rego on stdout."""
    from fortify.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["policy", "show-rego", str(policy_file)])
    rc = args.func(args)
    assert rc == 0
    assert "package fortify.policy" in capsys.readouterr().out
