"""Unit tests for the helpers that generate a starter policy at register time.

Two pure functions under test:

  * ``_classify_tool(name)`` — bucketing heuristic over a tool's name string.
  * ``_default_policy_for_manifest(manifest)`` — emits the role-aware YAML
    that lands on a brand-new ``Agent.policy_yaml`` on the first
    ``POST /v1/agents``.

The integration with ``register_manifest`` + the bundle-signing path is
covered in ``test_agents.py``; this file pins the contract of the
helpers in isolation so a regression here is easy to localize.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
import pytest

from hexgate_api.schemas import (
    AgentFramework,
    AgentManifest,
    InputSchema,
    SkillDefinition,
    SkillResources,
    ToolDefinition,
)
from hexgate_api.features.agents.compiler import (
    _classify_skill,
    _classify_tool,
    _default_policy_for_manifest,
    _emit_tool_lines,
    _yaml_key,
)

if TYPE_CHECKING:
    from hexgate.security.policy_set import PolicySet


# ---------------------------------------------------------------------------
# _classify_tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        # Read-shaped — substring/prefix matches against _READ_PATTERNS.
        ("read_file", "read"),
        ("web_search", "read"),
        ("fetch", "read"),
        ("list_users", "read"),
        ("get_order", "read"),
        ("find_record", "read"),
        ("grep", "read"),
        ("glob", "read"),
        ("describe_table", "read"),
        # Case-insensitive (the heuristic lowercases the input).
        # ``FetchAPI`` matches because ``fetch`` is a no-separator
        # substring pattern; ``ReadFile`` does NOT, see the explicit
        # known-limitation test below.
        ("FetchAPI", "read"),
        # Write-shaped.
        ("write_file", "write"),
        ("edit_file", "write"),
        ("create_user", "write"),
        ("update_record", "write"),
        ("delete_user", "write"),
        ("patch_object", "write"),
        # Shell-shaped — wins over write even if the name would also
        # match write patterns. Pin the precedence explicitly.
        ("bash", "shell"),
        ("shell_exec", "shell"),
        ("run_command", "shell"),
        ("subprocess_run", "shell"),
        # Unknown — anything that doesn't pattern-match. Caller treats
        # these as write-shape (fail-closed).
        ("refund_order", "unknown"),
        ("ping", "unknown"),
        ("custom_business_logic", "unknown"),
    ],
)
def test_classify_tool_buckets_by_name(name: str, expected: str) -> None:
    assert _classify_tool(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        # CamelCase names without underscores don't match the
        # ``read_`` / ``_read`` patterns. Documenting the limitation:
        # callers fail closed (unknown → write-shape) and surface the
        # tool in the heads-up comment, so the operator sees it and
        # reclassifies in the dashboard editor.
        "ReadFile",
        "WriteFile",
        # Brand-new business tools that don't fit any heuristic.
        "transfer_funds",
        "approve_loan",
    ],
)
def test_classify_tool_known_unclassifiable(name: str) -> None:
    """Pin the conservative-classifier contract — these all return
    ``unknown`` and the caller treats them as write-shape. If we ever
    loosen the heuristic, update or remove this test deliberately."""
    assert _classify_tool(name) == "unknown"


# ---------------------------------------------------------------------------
# _emit_tool_lines
# ---------------------------------------------------------------------------


def test_emit_tool_lines_renders_one_line_per_name() -> None:
    out = _emit_tool_lines(["read_file", "web_search"], mode="allow", indent=6)
    assert (
        out == "      read_file: { mode: allow }\n      web_search: { mode: allow }\n"
    )


def test_emit_tool_lines_returns_empty_string_for_empty_input() -> None:
    """Empty bucket → empty string so the caller can drop the surrounding
    ``tools:`` key cleanly (a ``tools:`` with no children is invalid)."""
    assert _emit_tool_lines([], mode="allow") == ""


# ---------------------------------------------------------------------------
# _default_policy_for_manifest — shape + round-trip
# ---------------------------------------------------------------------------


def _manifest(
    *tool_names: str, skills: list[SkillDefinition] | None = None
) -> AgentManifest:
    """Helper: build a minimal AgentManifest from a list of tool names."""
    empty_schema = InputSchema(properties={}, required=[])
    return AgentManifest(
        name="test_agent",
        framework=AgentFramework.LANGCHAIN,
        tools=[
            ToolDefinition(name=n, description=None, input_schema=empty_schema)
            for n in tool_names
        ],
        skills=skills,
    )


def test_default_policy_round_trips_through_policy_loader() -> None:
    """Generated YAML must parse cleanly via the SDK's policy loader —
    that's the same code path ``hexgate serve`` runs at every turn, so a
    parse failure here would brick freshly-registered agents."""
    from hexgate.security import load_policy_set_from_dict

    yaml_text = _default_policy_for_manifest(
        _manifest("web_search", "read_file", "write_file", "bash", "refund_order")
    )
    payload = yaml.safe_load(yaml_text)
    # Must not raise.
    policy_set = load_policy_set_from_dict(payload)
    # ``read_only`` is the mixin — the loader filters it out of the
    # publicly-selectable role list so the Playground's "Acting as"
    # dropdown shows operator-meaningful options only.
    assert sorted(policy_set.roles) == ["admin", "default", "member"]


def test_default_policy_admin_writes_allow_member_writes_approval() -> None:
    """The core differentiation between the two operator personas:
    member needs approval for writes, admin doesn't."""
    from hexgate.security import load_policy_set_from_dict

    payload = yaml.safe_load(
        _default_policy_for_manifest(_manifest("write_file", "edit_file"))
    )
    policy_set = load_policy_set_from_dict(payload)
    member = policy_set.policy_for("member")
    admin = policy_set.policy_for("admin")
    assert member.tools["write_file"].mode == "approval_required"
    assert member.tools["edit_file"].mode == "approval_required"
    assert admin.tools["write_file"].mode == "allow"
    assert admin.tools["edit_file"].mode == "allow"


def test_default_policy_shells_always_approval_required_even_for_admin() -> None:
    """Shells are the highest blast-radius primitive — the heuristic
    pins them at approval_required across both roles. If you want
    unattended shell access, you ask for it in the dashboard editor,
    not from the register-time default."""
    from hexgate.security import load_policy_set_from_dict

    payload = yaml.safe_load(
        _default_policy_for_manifest(_manifest("bash", "run_command"))
    )
    policy_set = load_policy_set_from_dict(payload)
    admin = policy_set.policy_for("admin")
    member = policy_set.policy_for("member")
    for tool_name in ("bash", "run_command"):
        assert admin.tools[tool_name].mode == "approval_required"
        assert member.tools[tool_name].mode == "approval_required"


def test_default_policy_reads_inherit_through_read_only_mixin() -> None:
    """Read-shape tools land in the read_only mixin and apply to every
    role that inherits it (default, member, admin)."""
    from hexgate.security import load_policy_set_from_dict

    payload = yaml.safe_load(
        _default_policy_for_manifest(_manifest("web_search", "read_file"))
    )
    policy_set = load_policy_set_from_dict(payload)
    for role_name in ("default", "member", "admin"):
        policy = policy_set.policy_for(role_name)
        assert policy.tools["web_search"].mode == "allow"
        assert policy.tools["read_file"].mode == "allow"


def test_default_policy_unknown_tools_fail_closed_for_member() -> None:
    """A tool that doesn't match any heuristic (e.g. ``refund_order``)
    is treated as write-shape — member: approval_required, admin: allow.
    Fail closed for the lower-trust role; surface to the operator via
    the heads-up comment so they can reclassify in the dashboard."""
    from hexgate.security import load_policy_set_from_dict

    yaml_text = _default_policy_for_manifest(_manifest("refund_order"))
    # Heads-up comment names the unclassified tool by name so the
    # operator notices in the dashboard editor.
    assert "refund_order" in yaml_text
    assert "could not classify" in yaml_text.lower()

    policy_set = load_policy_set_from_dict(yaml.safe_load(yaml_text))
    assert (
        policy_set.policy_for("member").tools["refund_order"].mode
        == "approval_required"
    )
    assert policy_set.policy_for("admin").tools["refund_order"].mode == "allow"


def test_default_policy_empty_manifest_still_parses() -> None:
    """An agent with zero declared tools generates a parseable policy —
    just the four role envelopes, no per-tool entries. Lets a dev
    register an agent skeleton before wiring tools in."""
    from hexgate.security import load_policy_set_from_dict

    payload = yaml.safe_load(_default_policy_for_manifest(_manifest()))
    policy_set = load_policy_set_from_dict(payload)
    assert sorted(policy_set.roles) == ["admin", "default", "member"]
    # No tools declared, so every role's ``tools`` map is empty.
    for role_name in ("default", "member", "admin"):
        assert policy_set.policy_for(role_name).tools == {}


def test_default_policy_does_not_overlap_member_admin_with_read_only() -> None:
    """A read tool should only appear in the read_only mixin block,
    not duplicated under member/admin. Keeps the YAML compact and
    avoids the dashboard editor showing the same tool three times."""
    yaml_text = _default_policy_for_manifest(_manifest("web_search", "write_file"))
    payload = yaml.safe_load(yaml_text)
    assert "web_search" in payload["roles"]["read_only"]["tools"]
    # The override blocks for member/admin only carry the write-shape
    # tools — the read is inherited, not restated.
    assert "web_search" not in payload["roles"]["member"].get("tools", {})
    assert "web_search" not in payload["roles"]["admin"].get("tools", {})


# ---------------------------------------------------------------------------
# Skills — _classify_skill + the generated ``skills:`` blocks
# ---------------------------------------------------------------------------


def _skill(
    name: str,
    *,
    scripts: list[str] | None = None,
    additional_tools: list[str] | None = None,
    enumerated: bool = True,
) -> SkillDefinition:
    resources = SkillResources(scripts=scripts or []) if enumerated else None
    return SkillDefinition(
        name=name,
        description=f"what {name} does",
        resources=resources,
        additional_tools=additional_tools or [],
    )


_PLAIN = _skill("runbook")
_SCRIPTED = _skill("exporter", scripts=["export.sh"])

# ``_default_policy_for_manifest(_manifest("web_search", "write_file", "bash",
# "refund_order"))`` captured before skills generation existed. Any drift here
# changes the starter policy of every agent that will never have a skill.
_PRE_SKILLS_STARTER_POLICY = """version: 1
# Generated by `hexgate register`. Edit freely — re-running register
# never overwrites this; it only updates the manifest snapshot.
#
# Four entries:
#   read_only  (mixin)  factored-out 'safe to read' allowlist
#   default             fallback when no User scope is set
#   member              typical user; writes + shells require approval
#   admin               power user; writes allow, shells still gate
#
# Note: 'admin' here is an AGENT policy role (used by the SDK at request
# time via User(role="admin")), distinct from the ORG admin role on
# /orgs/:id/members.

# Heuristic could not classify these tools — treating as writes
# (fail-closed). Move them to read_only or shells as appropriate:
#   - refund_order

roles:
  read_only:
    is_mixin: true
    default_policy:
      mode: deny
    tools:
      web_search: { mode: allow }

  default:
    inherits: [read_only]

  member:
    inherits: [read_only]
    tools:
      write_file: { mode: approval_required }
      refund_order: { mode: approval_required }
      bash: { mode: approval_required }

  admin:
    inherits: [read_only]
    tools:
      write_file: { mode: allow }
      refund_order: { mode: allow }
      bash: { mode: approval_required }
"""


def _policy_set(manifest: AgentManifest) -> PolicySet:
    from hexgate.security import load_policy_set_from_dict

    return load_policy_set_from_dict(
        yaml.safe_load(_default_policy_for_manifest(manifest))
    )


def test_default_policy_is_unchanged_for_a_manifest_without_skills() -> None:
    manifest = _manifest("web_search", "write_file", "bash", "refund_order")
    assert _default_policy_for_manifest(manifest) == _PRE_SKILLS_STARTER_POLICY


def test_classify_skill_gates_on_scripts() -> None:
    assert _classify_skill(_SCRIPTED) == "gated"


def test_classify_skill_gates_on_additional_tools() -> None:
    assert _classify_skill(_skill("widener", additional_tools=["issue_refund"])) == (
        "gated"
    )


def test_classify_skill_treats_unenumerated_resources_as_plain() -> None:
    """``resources=None`` (every deepagents skill) is missing information, not risk."""
    assert _classify_skill(_skill("deep", enumerated=False)) == "plain"
    assert _classify_skill(_PLAIN) == "plain"


def test_plain_skills_land_on_the_read_only_mixin() -> None:
    roles = yaml.safe_load(_default_policy_for_manifest(_manifest(skills=[_PLAIN])))[
        "roles"
    ]
    assert roles["read_only"]["skills"] == {"runbook": {"mode": "allow"}}
    assert "skills" not in roles["member"]
    assert "skills" not in roles["admin"]


def test_gated_skills_are_approval_for_member_and_allow_for_admin() -> None:
    policy_set = _policy_set(_manifest(skills=[_SCRIPTED]))
    assert (
        policy_set.policy_for("member").skills["exporter"].mode == "approval_required"
    )
    assert policy_set.policy_for("admin").skills["exporter"].mode == "allow"


@pytest.mark.parametrize("skills", [None, []])
def test_no_skills_key_emitted_when_there_are_none(
    skills: list[SkillDefinition] | None,
) -> None:
    """A dangling ``skills:`` with no children would fail the whole policy."""
    yaml_text = _default_policy_for_manifest(_manifest("web_search", skills=skills))
    assert "skills:" not in yaml_text


def test_generated_policy_round_trips_through_policy_loader() -> None:
    """Parses under ``extra="forbid"`` — only true once the SDK knows ``skills``."""
    policy_set = _policy_set(
        _manifest("web_search", "write_file", "bash", skills=[_PLAIN, _SCRIPTED])
    )
    assert sorted(policy_set.roles) == ["admin", "default", "member"]


def test_member_inherits_plain_skills_through_read_only() -> None:
    policy_set = _policy_set(_manifest(skills=[_PLAIN, _SCRIPTED]))
    for role_name in ("default", "member", "admin"):
        assert policy_set.policy_for(role_name).skills["runbook"].mode == "allow"


def test_gated_skill_is_absent_from_default_role() -> None:
    """Absent means closed-world denied for a caller with no role — not allowed."""
    policy_set = _policy_set(_manifest(skills=[_PLAIN, _SCRIPTED]))
    assert "exporter" not in policy_set.policy_for("default").skills


def test_skills_note_only_appears_when_there_are_skills() -> None:
    note = "# Skills discovered at registration."
    assert note not in _default_policy_for_manifest(_manifest("web_search"))
    assert note in _default_policy_for_manifest(_manifest(skills=[_PLAIN]))


def test_skills_note_describes_every_role_outcome_and_unenumerated_skills() -> None:
    """An operator must not read ``read_only`` as "instruction-only"."""
    yaml_text = _default_policy_for_manifest(_manifest(skills=[_PLAIN]))
    for fragment in ("'member'", "'admin'", "'default'", "not enumerated"):
        assert fragment in yaml_text


# ---------------------------------------------------------------------------
# Names YAML would otherwise coerce to a non-string key
# ---------------------------------------------------------------------------

_COERCED_NAMES = ["on", "yes", "null", "2024"]


@pytest.mark.parametrize("name", ["runbook", "read_file", "web-search"])
def test_yaml_key_leaves_ordinary_names_bare(name: str) -> None:
    assert _yaml_key(name) == name


@pytest.mark.parametrize("name", _COERCED_NAMES)
def test_yaml_key_quotes_names_yaml_would_coerce(name: str) -> None:
    assert yaml.safe_load(f"{_yaml_key(name)}: 0") == {name: 0}


def test_skill_names_yaml_would_coerce_still_load_as_strings() -> None:
    policy_set = _policy_set(
        _manifest(
            skills=[_skill(n) for n in _COERCED_NAMES]
            + [_skill("2025", scripts=["run.sh"])]
        )
    )
    admin = policy_set.policy_for("admin")
    assert set(admin.skills) == {*_COERCED_NAMES, "2025"}
    assert admin.skills["2025"].mode == "allow"


def test_tool_names_yaml_would_coerce_still_load_as_strings() -> None:
    policy_set = _policy_set(_manifest(*_COERCED_NAMES))
    assert set(policy_set.policy_for("member").tools) == set(_COERCED_NAMES)
