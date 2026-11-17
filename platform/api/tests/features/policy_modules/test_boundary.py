"""boundary_changes: which ceilings a set of draft files loosens or tightens."""

from __future__ import annotations

import json

import pytest
from hexgate.security.compose.grammar import (
    BoundaryBlock,
    CeilingSpec,
    ReachSpec,
    SkillCeilingSpec,
)
from hexgate.security.modules import LinkError

from hexgate_api.features.policy_modules import boundary
from hexgate_api.features.policy_modules.boundary import boundary_changes
from hexgate_api.features.policy_modules.seed_data import SEED_POLICY_FILES

_GRANTS = "agents:\n  bot:\n    roles:\n      support: { import: [ grants.yaml ] }\n"
W, T = "widened", "tightened"


def _entry(block: str) -> dict[str, str]:
    """A project whose ``policy.yaml`` holds ``block`` as its top-level boundary."""
    return {
        "policy.yaml": f"boundary:\n{block}{_GRANTS}",
        "grants.yaml": "tools: { refund: { mode: allow } }\n",
    }


def _tool(spec: str) -> dict[str, str]:
    return _entry(f"  tools:\n    refund: {spec}\n")


def _kinds(saved, drafted) -> list[tuple[str, str, str]]:
    return [(c.file, c.path, c.kind) for c in boundary_changes(saved, drafted)]


def _one_kind(before: str, after: str) -> str | None:
    """The kind of the one change between two specs of ``refund``, or None."""
    changes = boundary_changes(_tool(before), _tool(after))
    assert len(changes) <= 1
    return changes[0].kind if changes else None


def test_boundary_changes_happy_path() -> None:
    saved = _tool("{ mode: allow, constraint: 'args.amount <= 1000' }")
    drafted = _tool("{ mode: allow, constraint: 'args.amount <= 5000' }")
    [change] = boundary_changes(saved, drafted)
    assert change.file == "policy.yaml"
    assert change.path == "boundary.tools.refund"
    assert change.kind == W
    assert change.before == {"mode": "allow", "constraints": ["args.amount <= 1000"]}
    assert change.after == {"mode": "allow", "constraints": ["args.amount <= 5000"]}


def test_when_only_comments_change_then_no_changes() -> None:
    entry = SEED_POLICY_FILES["policy.yaml"]
    drafted = {**SEED_POLICY_FILES, "policy.yaml": f"# reviewed\n{entry}"}
    assert boundary_changes(SEED_POLICY_FILES, drafted) == []


def test_when_seed_refund_ceiling_is_raised_then_widened() -> None:
    entry = SEED_POLICY_FILES["policy.yaml"]
    drafted = {
        **SEED_POLICY_FILES,
        "policy.yaml": entry.replace('"args.amount <= 1000"', '"args.amount <= 99999"'),
    }
    assert _kinds(SEED_POLICY_FILES, drafted) == [
        ("policy.yaml", "boundary.tools.refund_order", W)
    ]


@pytest.mark.parametrize(
    ("before", "after", "kind"),
    [
        ("allow", "deny", T),
        ("approval_required", "deny", T),
        ("allow", "approval_required", T),
        ("deny", "approval_required", W),
        ("deny", "allow", W),
        ("approval_required", "allow", W),
    ],
)
def test_when_mode_moves_then_kind_follows(before: str, after: str, kind: str) -> None:
    assert _one_kind(f"{{ mode: {before} }}", f"{{ mode: {after} }}") == kind


def test_when_deny_condition_changes_then_no_change() -> None:
    # A deny in a ceiling leaves the tool ineligible whatever its condition says.
    assert (
        _one_kind("{ mode: deny, constraint: 'args.n > 10' }", "{ mode: deny }") is None
    )


@pytest.mark.parametrize(
    ("saved", "drafted", "expected"),
    [
        (
            "refund: {}",
            "refund: {}, wire: { mode: allow }",
            [("boundary.tools.wire", W)],
        ),
        ("refund: {}, wire: {}", "refund: {}", [("boundary.tools.wire", T)]),
        # Closed-world: an unlisted tool is already denied by the block.
        ("refund: {}", "refund: {}, wire: { mode: deny }", []),
    ],
)
def test_when_block_lists_other_tools_then_kind_follows(
    saved: str, drafted: str, expected: list
) -> None:
    changes = _kinds(
        _entry(f"  tools: {{ {saved} }}\n"), _entry(f"  tools: {{ {drafted} }}\n")
    )
    assert [(p, k) for _, p, k in changes] == expected


def test_when_admission_ceiling_is_added_then_widened() -> None:
    saved = _entry("  tools: { refund: {} }\n")
    drafted = _entry("  tools: { refund: {} }\n  admission: { mode: allow }\n")
    assert _kinds(saved, drafted) == [("policy.yaml", "boundary.admission", W)]


@pytest.mark.parametrize(
    ("saved", "drafted", "kind"),
    [
        # The gate engages only while some key of its kind is listed, so taking
        # the entry out may turn the gate off, whatever its mode was.
        ("admission: { mode: deny }", "", W),
        ("admission: { mode: allow }", "", W),
        ("reach: { billing: { as: tool } }", "", W),
        ("skills: { pdf: { mode: deny } }", "", W),
        # A deny arriving engages the gate and lets nothing more through.
        ("", "admission: { mode: deny }", T),
        ("", "skills: { pdf: { mode: deny } }", T),
        ("", "reach: { billing: { as: tool } }", W),
        ("", "admission: { mode: allow }", W),
        ("", "skills: { pdf: { mode: allow } }", W),
    ],
)
def test_when_gate_entry_comes_or_goes_then_kind_follows(saved, drafted, kind) -> None:
    def project(extra: str) -> dict[str, str]:
        return _entry(f"  tools: {{ refund: {{}} }}\n  {extra}\n")

    [change] = boundary_changes(project(saved), project(drafted))
    assert change.kind == kind


def test_when_gate_is_switched_off_then_resolution_admits_everyone() -> None:
    # Pins the premise above against the linker: with no grant naming admission,
    # the boundary's deny entry is what keeps the admission gate engaged.
    from hexgate_api.features.policy_modules.diff import _resolve

    def declares(files: dict[str, str]) -> bool:
        return _resolve(files)["*"].policy_set.declares_admission()

    saved = _entry("  tools: { refund: {} }\n  admission: { mode: deny }\n")
    drafted = _entry("  tools: { refund: {} }\n")
    assert declares(saved) and not declares(drafted)
    assert [c.kind for c in boundary_changes(saved, drafted)] == [W]


def test_when_block_is_removed_then_widened() -> None:
    saved = _tool("{}")
    [change] = boundary_changes(saved, {**saved, "policy.yaml": _GRANTS})
    assert (change.path, change.kind, change.after) == ("boundary", W, None)
    assert change.before["tools"]["refund"]["mode"] == "allow"


@pytest.mark.parametrize(
    ("saved", "drafted", "expected"),
    [
        # A block added to policy.yaml, then a new file that holds one.
        ({"policy.yaml": _GRANTS}, {}, ("policy.yaml", "boundary", T)),
        (
            {},
            {"caps/c.yaml": "boundary: { tools: { refund: {} } }\n"},
            ("caps/c.yaml", "boundary", T),
        ),
    ],
)
def test_when_block_is_added_then_tightened(saved, drafted, expected) -> None:
    base = _tool("{}")
    assert _kinds({**base, **saved}, {**base, **drafted}) == [expected]


def test_when_only_capabilities_change_then_no_changes() -> None:
    saved = _tool("{}")
    drafted = {
        **saved,
        "grants.yaml": "tools: { refund: { mode: allow }, wire: { mode: allow } }\n",
        "policy.yaml": saved["policy.yaml"].replace("[ grants.yaml ]", "[ g.yaml ]"),
    }
    assert boundary_changes(saved, drafted) == []


def test_when_block_in_another_file_widens_then_found() -> None:
    # The SDK refuses a boundary in an imported fragment, so this file is not in
    # force; it is still reported, which can only over-report, never miss one.
    cap = "boundary:\n  tools:\n    refund: { constraint: 'args.amount <= 200' }\n"
    saved = {**_tool("{}"), "caps/cap.yaml": cap}
    drafted = {**saved, "caps/cap.yaml": cap.replace("200", "2000")}
    assert _kinds(saved, drafted) == [("caps/cap.yaml", "boundary.tools.refund", W)]


@pytest.mark.parametrize(
    ("path", "template"),
    [
        ("agents.bot.boundary", "agents:\n  bot: {{ {} }}\n"),
        (
            "agents.bot.roles.s.boundary",
            "agents:\n  bot:\n    roles:\n      s: {{ {} }}\n",
        ),
        ("export.desk.boundary", "export:\n  desk: {{ {} }}\n"),
    ],
)
def test_when_block_at_any_level_widens_then_found(path: str, template: str) -> None:
    def text(cap: int) -> dict[str, str]:
        block = (
            f"boundary: {{ tools: {{ refund: {{ constraint: 'args.n <= {cap}' }} }} }}"
        )
        return {"policy.yaml": template.format(block)}

    assert _kinds(text(100), text(900)) == [("policy.yaml", f"{path}.tools.refund", W)]


def _spec(constraints: list[str], mode: str = "allow") -> str:
    return json.dumps({"mode": mode, "constraints": constraints})


@pytest.mark.parametrize(
    ("before", "after", "kind"),
    [
        # A numeric ceiling moved, or a constraint removed or added.
        (["args.amount <= 1000"], ["args.amount <= 500"], T),
        (["args.amount <= 10"], [], W),
        ([], ['args.currency == "EUR"'], T),
        (['args.currency == "EUR"'], [], W),
        # Strictness at an equal bound, and lower bounds, which move the other way.
        (["args.n < 10"], ["args.n <= 10"], W),
        (["args.n <= 10"], ["args.n < 10"], T),
        (["args.n >= 5"], ["args.n >= 1"], W),
        (["args.n >= 1"], ["args.n > 1"], T),
        # The literal on the left reads as on the right; a count cap is a bound.
        (["args.n <= 10"], ["5 >= args.n"], T),
        (["count(args.to) <= 3"], ["count(args.to) <= 30"], W),
        # The tightest of two bounds on one subject is the one that counts.
        (["args.n <= 10", "args.n <= 50"], ["args.n <= 10"], None),
        (["args.n >= 1", "args.n >= 5"], ["args.n >= 5"], None),
        # A count cap is a bound; an infinite literal is the loosest bound.
        (["count(args.to) <= 30"], ["count(args.to) <= 3"], T),
        (["args.n <= 1e999"], ["args.n <= 10"], T),
        # An allowlist grown, shrunk or swapped; an exclusion list the other way.
        (['args.c in ["EUR"]'], ['args.c in ["EUR", "USD"]'], W),
        (['args.c in ["EUR", "USD"]'], ['args.c in ["EUR"]'], T),
        (['args.c in ["EUR"]'], ['args.c in ["USD"]'], W),
        (['args.c not in ["X", "Y"]'], ['args.c not in ["X"]'], W),
        (['args.c not in ["X"]'], ['args.c not in ["X", "Y"]'], T),
        # One part loosened and another tightened: the loosening is reported.
        (["args.n <= 10"], ["args.n <= 99", "args.m <= 1"], W),
        # Two conditions of one kind on one subject combine before comparing.
        (['args.c in ["A", "B"]', 'args.c in ["B", "C"]'], ['args.c in ["B"]'], None),
        (
            ['args.c not in ["X"]', 'args.c not in ["Y"]'],
            ['args.c not in ["X", "Y"]'],
            None,
        ),
        (["args.a == 1", "args.a == 1"], ["args.a == 1"], None),
        (["args.max >= args.min"] * 2, ["args.max >= args.min"], None),
        # A cross-field comparison, an "or" and a condition the parser rejects
        # are each matched as written.
        (["args.max >= args.min"], [], W),
        (['args.c != "X"'], [], W),
        (['args.s <= "m"'], ['args.s  <=  "m"'], None),
        (["args.n <= 10 or args.vip == true"], ["args.n <= 99 or args.vip == true"], W),
        (["args.n ~~ 1"], ["  args.n ~~ 1 "], None),
        (["args.n ~~ 1"], [], W),
        # An equality is an allowlist of one, an inequality an exclusion of one.
        (['args.c != "X"', 'args.c != "Y"'], ['args.c not in ["X", "Y"]'], None),
        (['args.c != "X"'], ['args.c not in ["X", "Y"]'], T),
        (['args.c in ["A", "B"]'], ['args.c == "A"'], T),
        (['args.c == "A"'], ['args.c in ["A", "B"]'], W),
        # Formatting only; a literal's own spacing is not formatting.
        (["(args.a == 1) or (args.b == 2)"], ["args.a == 1 or args.b == 2"], None),
        (['args.s == "a  b" or args.v == 1'], ['args.s == "a b" or args.v == 1'], W),
        (["args.n<=10"], ["args.n <= 10"], None),
        (['startswith(args.id,"a")'], ['startswith(args.id, "a")'], None),
        (["(args.n <= 10) and (args.m <= 1)"], ["args.m <= 1", "args.n <= 10"], None),
    ],
)
def test_when_constraints_change_then_kind_follows(
    before: list[str], after: list[str], kind: str | None
) -> None:
    assert _one_kind(_spec(before), _spec(after)) == kind


def test_when_mode_tightens_but_cap_rises_then_widened() -> None:
    before = _spec(["args.n <= 10"])
    assert _one_kind(before, _spec(["args.n <= 99"], "approval_required")) == W


@pytest.mark.parametrize(
    ("section", "name", "before", "after", "kind"),
    [
        ("reach", "billing", "{ as: tool }", "{ as: [tool, handoff] }", W),
        # Each reach mode has its own gate, so one leaving may switch it off.
        ("reach", "billing", "{ as: [tool, handoff] }", "{ as: tool }", W),
        ("reach", "billing", "{ as: [tool, handoff] }", "{ as: handoff }", W),
        (
            "skills",
            "pdf",
            "{ via: [instructions, resource] }",
            "{ via: instructions }",
            T,
        ),
        ("admission", "", "{ mode: allow }", "{ mode: deny }", T),
        # A deny stays a deny whatever else changes: its key stays listed.
        (
            "admission",
            "",
            "{ mode: deny, constraint: 'ctx.x == 1' }",
            "{ mode: deny }",
            None,
        ),
        (
            "skills",
            "pdf",
            "{ mode: deny, via: [instructions, resource] }",
            "{ mode: deny, via: instructions }",
            None,
        ),
        ("skills", "pdf", "{ mode: approval_required }", "{ mode: allow }", W),
        ("reach", "billing", "{ as: tool }", "{ as: handoff }", W),
        (
            "reach",
            "billing",
            "{ as: tool, constraint: 'run.calls <= 1' }",
            "{ as: tool, constraint: 'run.calls <= 9' }",
            W,
        ),
        (
            "skills",
            "pdf",
            "{ via: instructions }",
            "{ via: [instructions, resource] }",
            W,
        ),
    ],
)
def test_when_gate_entry_changes_then_kind_follows(
    section, name, before, after, kind
) -> None:
    def project(spec: str) -> dict[str, str]:
        return _entry(
            f"  {section}:\n    {name}: {spec}\n" if name else f"  {section}: {spec}\n"
        )

    path = f"boundary.{section}.{name}" if name else f"boundary.{section}"
    expected = [("policy.yaml", path, kind)] if kind else []
    assert _kinds(project(before), project(after)) == expected


def test_when_reach_mode_leaves_then_its_gate_can_switch_off() -> None:
    # Pins the reach rule against the linker: the ceiling's as-tool mode is what
    # keeps the as-tool reach gate engaged when grants reach by handoff only.
    from hexgate_api.features.policy_modules.diff import _resolve

    def project(modes: str) -> dict[str, str]:
        return {
            "policy.yaml": f"boundary: {{ reach: {{ billing: {{ as: {modes} }} }} }}\n"
            "tools: { reach_grant: {} }\n"
            "reach: { billing: { as: handoff } }\n"
        }

    def tool_reach(files: dict[str, str]) -> bool:
        return _resolve(files)["*"].policy_set.declares_tool_reach()

    saved, drafted = project("[tool, handoff]"), project("handoff")
    assert tool_reach(saved) and not tool_reach(drafted)
    assert [c.kind for c in boundary_changes(saved, drafted)] == [W]


@pytest.mark.parametrize("spec", [CeilingSpec, ReachSpec, SkillCeilingSpec])
def test_when_entry_spec_has_a_field_then_it_is_compared(spec) -> None:
    # An entry field the comparison does not read would loosen with no report.
    assert set(spec.model_fields) <= {"mode", "constraints", "via"}


def test_when_boundary_has_a_field_then_it_is_compared() -> None:
    # A ceiling field the comparison does not read would loosen with no report.
    compared = {*boundary._SECTIONS, "admission"}
    assert compared == set(BoundaryBlock.model_fields)


def test_when_changed_file_does_not_parse_then_link_error() -> None:
    saved = _tool("{}")
    with pytest.raises(LinkError):
        boundary_changes(saved, {**saved, "policy.yaml": "boundary: ["})


def test_when_unchanged_file_does_not_parse_then_it_is_skipped() -> None:
    saved = {**_tool("{}"), "broken.yaml": "boundary: ["}
    assert boundary_changes(saved, dict(saved)) == []
