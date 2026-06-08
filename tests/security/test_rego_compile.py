"""Tests for the YAML → Rego compiler (M2 phase 1).

Two kinds of checks here:

  * Structural / golden tests on the emitted Rego source — operators,
    package header, rule-head naming, deterministic ordering.

  * Parity tests: for a given role + tool + args input, the rules that
    *would* fire in Rego (as predicted by the source structure) match
    the decision today's pydantic :func:`authorize_tool_call` returns.
    These are predictive — when the wasmtime-py adapter lands in a later
    phase, the same fixtures become true end-to-end parity checks.
"""

from __future__ import annotations

import re

import pytest
import yaml

from fortify.security import (
    AgentPolicy,
    PolicyDeniedError,
    PolicySetError,
    authorize_tool_call,
    compile_default_only,
    compile_to_rego,
    load_policy_set_from_dict,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


_SUPPORT_BOT_POLICY = yaml.safe_load(
    """
version: 1
roles:
  read_only:
    is_mixin: true
    tools:
      web_search: { mode: allow }
      read_file:  { mode: allow }

  default:
    inherits: [read_only]
    tools:
      refund_order: { mode: deny }

  support:
    inherits: [read_only]
    tools:
      refund_order:
        mode: allow
        constraints:
          - args.amount <= 50
          - args.currency == "USD"

  billing:
    inherits: [read_only]
    tools:
      refund_order:
        mode: allow
        constraints:
          - args.amount <= 500
          - args.currency in ["USD", "EUR"]
"""
)


def _allow_rules(rego: str) -> list[str]:
    """Split the emitted Rego on the ``allow if {`` heading.

    Returns the rule bodies (the content between ``{`` and the matching
    ``}``) so tests can inspect each rule's conditions without dragging
    in a Rego parser.
    """
    out: list[str] = []
    for match in re.finditer(r"allow if \{\n(.*?)\n\}", rego, re.DOTALL):
        out.append(match.group(1))
    return out


def _approval_rules(rego: str) -> list[str]:
    out: list[str] = []
    for match in re.finditer(r"requires_approval if \{\n(.*?)\n\}", rego, re.DOTALL):
        out.append(match.group(1))
    return out


def _violation_rules(rego: str) -> list[str]:
    """Return each ``violations contains ... if { ... }`` rule's body.

    The membership value can be a backtick raw-string (which the emitter
    prefers, since it skips escape processing) or a JSON-escaped double-
    quoted string for the rare backtick-containing case. Match both
    flavours and the sentinel; let the caller filter the sentinel out.
    """
    out: list[str] = []
    pattern = re.compile(
        r"violations contains (?:`[^`]+`|\"[^\"]*\") if \{\n(.*?)\n\}",
        re.DOTALL,
    )
    for match in pattern.finditer(rego):
        out.append(match.group(1))
    return out


# ---------------------------------------------------------------------------
# Header / module structure
# ---------------------------------------------------------------------------


def test_emits_package_header_and_defaults() -> None:
    """Module starts with the package declaration + both default rules."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    assert "package fortify.policy" in rego
    assert "default allow := false" in rego
    assert "default requires_approval := false" in rego


def test_custom_package_name_carries_through() -> None:
    """Caller can override the package name (M3 will use this per-agent)."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY, package="fortify.policy.support_bot")
    assert "package fortify.policy.support_bot" in rego


def test_emits_source_hash_in_header() -> None:
    """The header records the sha256 of the source payload for traceability."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    assert re.search(r"sha256: [0-9a-f]{64}\b", rego), rego


def test_explicit_source_hash_is_used_verbatim() -> None:
    """Passing source_hash overrides the auto-computed one (CLI uses this)."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY, source_hash="deadbeef" * 8)
    assert "deadbeef" in rego


# ---------------------------------------------------------------------------
# Role / tool emission semantics
# ---------------------------------------------------------------------------


def test_emits_allow_rule_per_role_and_tool() -> None:
    """Each (role, tool) with mode=allow emits exactly one allow rule."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    rules = _allow_rules(rego)
    # support_bot: 3 roles (read_only is mixin, dropped) × 3 tools (web_search,
    # read_file each get an allow per role; refund_order is allow for support
    # + billing, deny for default).
    #   default  → web_search, read_file (refund_order is deny, no rule)
    #   support  → web_search, read_file, refund_order (with 2 constraints)
    #   billing  → web_search, read_file, refund_order (with 2 constraints)
    assert len(rules) == 2 + 3 + 3


def test_mixin_role_omitted_from_output() -> None:
    """Mixin roles don't surface as concrete roles in the Rego output."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    assert 'input.role == "read_only"' not in rego


def test_deny_tool_emits_no_rule() -> None:
    """`mode: deny` produces no rule — absence of allow IS the deny."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    # default.refund_order is deny — no allow rule should reference it
    # alongside input.role == "default".
    for rule in _allow_rules(rego):
        if 'input.role == "default"' in rule:
            assert "refund_order" not in rule, rule


def test_role_section_comments_present() -> None:
    """Each role gets a ``# ---- role: NAME ----`` divider — readability."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    assert "# ---- role: billing" in rego
    assert "# ---- role: default" in rego
    assert "# ---- role: support" in rego
    # mixin section never gets emitted
    assert "# ---- role: read_only" not in rego


def test_output_is_deterministic_across_runs() -> None:
    """Same input → identical bytes. Critical for content-addressing bundles."""
    a = compile_to_rego(_SUPPORT_BOT_POLICY)
    b = compile_to_rego(_SUPPORT_BOT_POLICY)
    assert a == b


def test_roles_emitted_in_alphabetical_order() -> None:
    """Role sections sort alphabetically regardless of dict insertion order."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    billing = rego.index("# ---- role: billing")
    default = rego.index("# ---- role: default")
    support = rego.index("# ---- role: support")
    assert billing < default < support


# ---------------------------------------------------------------------------
# Constraint translation
# ---------------------------------------------------------------------------


def test_numeric_constraint_prefixes_input() -> None:
    """``args.amount <= 50`` → ``input.args.amount <= 50``."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    # support's refund_order has args.amount <= 50
    support_rules = [r for r in _allow_rules(rego) if '"support"' in r]
    refund_rule = next(r for r in support_rules if '"refund_order"' in r)
    assert "input.args.amount <= 50" in refund_rule


def test_string_equality_constraint() -> None:
    """JSON-double-quoted strings survive the translation intact."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    support_rules = [r for r in _allow_rules(rego) if '"support"' in r]
    refund_rule = next(r for r in support_rules if '"refund_order"' in r)
    assert 'input.args.currency == "USD"' in refund_rule


def test_in_list_constraint() -> None:
    """``args.X in ["a", "b"]`` translates verbatim (Rego has the same op)."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    billing_rules = [r for r in _allow_rules(rego) if '"billing"' in r]
    refund_rule = next(r for r in billing_rules if '"refund_order"' in r)
    assert 'input.args.currency in ["USD", "EUR"]' in refund_rule


def test_not_in_constraint_wraps_with_not() -> None:
    """``not in`` becomes Rego's ``not X in Y`` (semantically equivalent)."""
    payload = {
        "version": 1,
        "roles": {
            "default": {
                "tools": {
                    "refund": {
                        "mode": "allow",
                        "constraints": ['args.priority not in ["urgent"]'],
                    }
                }
            }
        },
    }
    rego = compile_to_rego(payload)
    assert 'not input.args.priority in ["urgent"]' in rego


def test_compile_rejects_unparseable_constraint() -> None:
    """An invalid constraint surfaces at compile time, not at WASM eval."""
    payload = {
        "version": 1,
        "roles": {
            "default": {
                "tools": {
                    "refund": {
                        "mode": "allow",
                        "constraints": ["args.amount ~~ 50"],
                    }
                }
            }
        },
    }
    with pytest.raises(PolicySetError, match="invalid constraint"):
        compile_to_rego(payload)


# ---------------------------------------------------------------------------
# Approval-required mode
# ---------------------------------------------------------------------------


def test_approval_required_emits_separate_rule_head() -> None:
    """``mode: approval_required`` produces a ``requires_approval`` rule,
    not an ``allow`` rule — the runtime queries both and dispatches the
    approval handler when this one fires."""
    payload = {
        "version": 1,
        "roles": {
            "default": {
                "tools": {
                    "issue_credit": {
                        "mode": "approval_required",
                        "constraints": ["args.amount <= 500"],
                    }
                }
            }
        },
    }
    rego = compile_to_rego(payload)
    assert len(_allow_rules(rego)) == 0
    [approval] = _approval_rules(rego)
    assert 'input.tool == "issue_credit"' in approval
    assert "input.args.amount <= 500" in approval


# ---------------------------------------------------------------------------
# Shape variants
# ---------------------------------------------------------------------------


def test_flat_single_policy_compiles_as_default_role() -> None:
    """Legacy flat ``policy.yaml`` (no ``roles:`` key) wraps as default."""
    payload = {
        "version": 1,
        "tools": {
            "web_search": {"mode": "allow"},
        },
    }
    rego = compile_to_rego(payload)
    [rule] = _allow_rules(rego)
    assert 'input.role == "default"' in rule
    assert 'input.tool == "web_search"' in rule


def test_compile_default_only_wraps_AgentPolicy() -> None:
    """Convenience wrapper for callers that already hold an AgentPolicy."""
    policy = AgentPolicy.model_validate({"tools": {"web_search": {"mode": "allow"}}})
    rego = compile_default_only(policy)
    assert 'input.tool == "web_search"' in rego


def test_empty_inline_roles_compiles_to_default_only() -> None:
    """A payload with ``roles:`` but no concrete roles still produces a module
    with the default rules (no allow rules, just the headers)."""
    # All-mixin policy_map raises today (load_policy_set_from_dict via
    # load_policy_map). The compiler surfaces that as the same error type.
    payload = {
        "version": 1,
        "roles": {
            "mix": {"is_mixin": True, "tools": {"web_search": {"mode": "allow"}}}
        },
    }
    with pytest.raises(PolicySetError):
        compile_to_rego(payload)


# ---------------------------------------------------------------------------
# Structured decision object (M2 phase 3.5)
# ---------------------------------------------------------------------------


def test_emits_decision_object_with_all_three_fields() -> None:
    """The module's single entrypoint is `decision := {allow, requires_approval, violations}`."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    assert "decision := {" in rego
    assert '"allow": allow,' in rego
    assert '"requires_approval": requires_approval,' in rego
    assert '"violations": violations,' in rego


def test_emits_rego_v1_import() -> None:
    """Modern opa needs `import rego.v1` for the contains / if syntax."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    assert "import rego.v1" in rego


def test_emits_violations_rule_per_constraint() -> None:
    """Each constraint emits its own `violations contains <raw> if {...}` rule."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    # support_bot has 4 constraints total:
    # support.refund_order: amount <= 50, currency == "USD"
    # billing.refund_order: amount <= 500, currency in ["USD","EUR"]
    assert len(_violation_rules(rego)) == 4


def test_violation_rule_uses_raw_constraint_string() -> None:
    """The membership value is the original YAML string verbatim — that's
    the dev's deny reason at runtime."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    assert "violations contains `args.amount <= 500` if" in rego
    assert "violations contains `args.amount <= 50` if" in rego
    assert 'violations contains `args.currency in ["USD", "EUR"]` if' in rego
    assert 'violations contains `args.currency == "USD"` if' in rego


def test_violation_rule_body_negates_constraint() -> None:
    """The rule body matches role/tool and asserts `not <constraint>`."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    # Pick out billing's amount violation rule and inspect.
    pattern = re.compile(
        r"violations contains `args\.amount <= 500` if \{\n(.*?)\n\}", re.DOTALL
    )
    [body] = pattern.findall(rego)
    assert 'input.role == "billing"' in body
    assert 'input.tool == "refund_order"' in body
    assert "not input.args.amount <= 500" in body


def test_violations_sentinel_emitted_for_constraint_free_policy() -> None:
    """Policies with zero constraints still need `violations` defined —
    a `false`-bodied sentinel keeps the decision rule safe to build."""
    payload = {
        "version": 1,
        "roles": {
            "default": {"tools": {"web_search": {"mode": "allow"}}},
        },
    }
    rego = compile_to_rego(payload)
    assert 'violations contains "__never__" if false' in rego


def test_decision_default_includes_empty_state() -> None:
    """`default allow`/`default requires_approval` cover the fall-through;
    `violations` defaults to the empty set by construction."""
    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    assert "default allow := false" in rego
    assert "default requires_approval := false" in rego


# ---------------------------------------------------------------------------
# Parity with the pydantic engine
#
# Two parametrized tests share the same fixture table:
#
#   * The "predictive" one walks the rule structure with regex + the same
#     parse_constraint engine the SDK enforces with. Cheap, no opa needed,
#     catches emitter regressions.
#
#   * The "semantic" one compiles the rego all the way to WASM and runs it
#     through the wasm_engine. This is the load-bearing check — when this
#     matches pydantic, phase 6's enforcement cutover is a flag flip.
# ---------------------------------------------------------------------------

_PARITY_CASES: list[tuple[str, str, dict, bool]] = [
    ("billing", "refund_order", {"amount": 30, "currency": "USD"}, True),
    ("billing", "refund_order", {"amount": 600, "currency": "USD"}, False),
    ("billing", "refund_order", {"amount": 30, "currency": "JPY"}, False),
    ("support", "refund_order", {"amount": 30, "currency": "USD"}, True),
    ("support", "refund_order", {"amount": 200, "currency": "USD"}, False),
    ("support", "refund_order", {"amount": 30, "currency": "EUR"}, False),
    ("default", "refund_order", {"amount": 5, "currency": "USD"}, False),
    ("billing", "web_search", {}, True),
    ("default", "web_search", {}, True),
]


@pytest.mark.parametrize(("role", "tool", "args", "expect_allow"), _PARITY_CASES)
def test_parity_predicted_rego_vs_pydantic(
    role: str, tool: str, args: dict, expect_allow: bool
) -> None:
    """Cheap structural parity — no opa needed.

    Walks the emitted rules with regex and re-evaluates each constraint
    with the SDK's :func:`parse_constraint` engine. Catches emitter bugs
    (wrong operator, wrong path) without needing a wasm runtime.
    """
    ps = load_policy_set_from_dict(_SUPPORT_BOT_POLICY)
    policy = ps.policy_for(role)
    try:
        authorize_tool_call(policy, tool, args)
        py_allow = True
    except PolicyDeniedError:
        py_allow = False
    assert py_allow is expect_allow, (
        f"pydantic engine disagrees for {role}/{tool}/{args}"
    )

    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    rego_allow = _predict_rego_allow(rego, role, tool, args)
    assert rego_allow is expect_allow, (
        f"emitted Rego predicts the wrong decision for {role}/{tool}/{args}"
    )


# Compile the wasm once for all semantic-parity cases — opa build takes
# ~100ms, multiplied by 9 cases that's a noticeable slice of the suite.
@pytest.fixture(scope="module")
def _support_bot_wasm() -> bytes:
    import shutil

    if shutil.which("opa") is None:
        pytest.skip("opa not on PATH")
    from fortify.security import compile_to_wasm

    rego = compile_to_rego(_SUPPORT_BOT_POLICY)
    return compile_to_wasm(rego).wasm


@pytest.mark.parametrize(("role", "tool", "args", "expect_allow"), _PARITY_CASES)
def test_parity_wasm_vs_pydantic(
    role: str, tool: str, args: dict, expect_allow: bool, _support_bot_wasm: bytes
) -> None:
    """Semantic parity — what the wasm runtime would actually decide.

    This is the load-bearing check for the enforcement cutover in M2 phase
    6. If this matches pydantic for every input shape we care about, we
    can swap the enforcer with confidence.
    """
    from fortify.security import WasmPolicy

    ps = load_policy_set_from_dict(_SUPPORT_BOT_POLICY)
    policy = ps.policy_for(role)
    try:
        authorize_tool_call(policy, tool, args)
        py_allow = True
    except PolicyDeniedError:
        py_allow = False
    assert py_allow is expect_allow, (
        f"pydantic engine disagrees for {role}/{tool}/{args}"
    )

    wasm_policy = WasmPolicy.from_bytes(_support_bot_wasm)
    decision = wasm_policy.decide(role=role, tool=tool, args=args)
    assert decision.allow is expect_allow, (
        f"wasm engine disagrees with pydantic for {role}/{tool}/{args}: "
        f"got {decision}, expected allow={expect_allow}"
    )


def _predict_rego_allow(rego: str, role: str, tool: str, args: dict) -> bool:
    """Lightweight Rego eval substitute for the parity test.

    Scans emitted rules for one whose role + tool match the input, and
    then re-evaluates each constraint line using the pydantic
    :func:`parse_constraint` engine — which is the same parser the SDK
    enforces with. When the wasm adapter ships, this stub gets replaced
    by a real ``wasm_module.evaluate(input)`` call.
    """
    from fortify.security.constraints import evaluate_constraint, parse_constraint

    for rule in _allow_rules(rego):
        if f'input.role == "{role}"' not in rule:
            continue
        if f'input.tool == "{tool}"' not in rule:
            continue
        # Re-derive the args.* constraint lines: strip the leading ``input.``,
        # leaving the original constraint grammar for parse_constraint.
        ok = True
        for line in rule.splitlines():
            stripped = line.strip()
            if (
                not stripped
                or stripped.startswith("input.role")
                or stripped.startswith("input.tool")
            ):
                continue
            # Unwrap the ``not X in Y`` shape back into our grammar.
            if stripped.startswith("not input."):
                stripped = stripped.replace("not input.", "", 1) + ""
                stripped = stripped.replace(" in ", " not in ")
            else:
                stripped = stripped.replace("input.", "", 1)
            constraint = parse_constraint(stripped)
            if not evaluate_constraint(constraint, {"args": args}):
                ok = False
                break
        if ok:
            return True
    return False
