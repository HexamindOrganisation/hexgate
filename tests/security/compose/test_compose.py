"""Tests for the ``policy.yaml`` compose front-end.

The front-end lowers the position-wildcard grammar to the linker's ModuleContent
lists, so the headline test is **golden parity**: a compose policy and the
equivalent tier-folder project resolve to byte-identical effective policy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hexgate.security import (
    AgentPolicy,
    BaseToolPolicy,
    LinkError,
    ModuleContent,
    resolve_for_project,
)
from hexgate.security.compose import parse_entry, resolve_file, resolve_text
from hexgate.security.linker import effective_policy_by_role


def _eff(res):
    return effective_policy_by_role(res)


# --- parse ---------------------------------------------------------------


def test_parse_worked_example():
    entry = parse_entry(
        """
        version: 1
        boundary:
          tools: { refund_order: { constraint: "args.amount <= 5000" } }
        tools: { read_ticket: { mode: allow } }
        agents:
          triage_bot:
            roles:
              support:
                reach: { billing_bot: { as: tool, constraint: "args.amount <= 1000" } }
        """
    )
    assert list(entry.tools) == ["read_ticket"]
    reach = entry.agents["triage_bot"].roles["support"].reach["billing_bot"]
    assert reach.via == ["tool"]
    assert reach.constraints == ["args.amount <= 1000"]  # singular folded to list


def test_reserved_agent_name_rejected():
    with pytest.raises(LinkError, match="reserved"):
        parse_entry("agents: { tools: {} }")


def test_reserved_role_name_rejected():
    with pytest.raises(LinkError, match="reserved"):
        parse_entry("agents: { bot: { roles: { boundary: {} } } }")


def test_invalid_yaml_is_a_linkerror():
    with pytest.raises(LinkError, match="invalid YAML"):
        parse_entry("tools: { x: { : }")  # malformed mapping


def test_empty_via_rejected():
    # `as: []` is an empty transfer-mode list; AgentTargetPolicy rejects it, and
    # resolve surfaces that as a source-named LinkError.
    with pytest.raises(LinkError):
        resolve_text("reach: { billing_bot: { as: [] } }")


def test_unknown_top_level_key_rejected():
    with pytest.raises(LinkError):
        parse_entry("tolls: { x: {} }")  # typo → extra=forbid


def test_non_mapping_document_rejected():
    with pytest.raises(LinkError, match="mapping"):
        parse_entry("- just\n- a\n- list")


def test_falsy_non_mapping_documents_rejected():
    # A falsy non-mapping (empty list, bool, number) must error like any other
    # non-mapping, not silently coerce to an empty policy.
    for doc in ("[]", "false", "42"):
        with pytest.raises(LinkError, match="mapping"):
            parse_entry(doc)


def test_empty_or_null_document_is_an_empty_deny_all_policy():
    # A comment-only / explicitly-null file is a valid empty (fail-closed) policy.
    res = resolve_text("null")
    assert sorted(_eff(res)) == ["default"]
    assert _eff(res)["default"]["tools"] == {}


# --- resolve: roles + scope ---------------------------------------------


def test_default_role_always_present():
    res = resolve_text("tools: { a: { mode: allow } }")
    assert sorted(_eff(res)) == ["default"]


def test_named_agent_resolves_its_roles_plus_default():
    doc = """
    agents:
      bot:
        roles:
          support: { tools: { a: { mode: allow } } }
          billing: { tools: { b: { mode: allow } } }
    """
    assert sorted(_eff(resolve_text(doc, agent="bot"))) == [
        "billing",
        "default",
        "support",
    ]


def test_scope_by_depth_agent_and_role_grants_fold_together():
    doc = """
    tools: { top: { mode: allow } }
    agents:
      bot:
        tools: { agentwide: { mode: allow } }
        roles:
          support: { tools: { rolescoped: { mode: allow } } }
    """
    support = _eff(resolve_text(doc, agent="bot"))["support"]["tools"]
    assert set(support) == {"top", "agentwide", "rolescoped"}
    # the default role gets the base scopes only (no role-scoped grant)
    default = _eff(resolve_text(doc, agent="bot"))["default"]["tools"]
    assert set(default) == {"top", "agentwide"}


# --- reach (composes via #124) ------------------------------------------


def test_reach_allowed_when_boundary_permits_it():
    doc = """
    boundary:
      reach: { billing_bot: { as: tool, constraint: "args.amount <= 2000" } }
    agents:
      bot:
        roles:
          support:
            reach: { billing_bot: { as: tool, constraint: "args.amount <= 1000" } }
    """
    reach = _eff(resolve_text(doc, agent="bot"))["support"]["tools"][
        "agent.tool:billing_bot"
    ]
    assert reach["mode"] == "allow"
    # ceiling AND grant, both present
    assert any("2000" in c for c in reach["constraints"])
    assert any("1000" in c for c in reach["constraints"])


def test_reach_multi_via_lowers_to_both_keys():
    # `as: [tool, handoff]` lowers to both agent.tool: and agent.handoff: keys,
    # each permitted by a boundary that lists the same two vias.
    doc = """
    boundary:
      reach: { billing_bot: { as: [tool, handoff] } }
    agents:
      bot:
        roles:
          support:
            reach: { billing_bot: { as: [tool, handoff] } }
    """
    tools = _eff(resolve_text(doc, agent="bot"))["support"]["tools"]
    assert tools["agent.tool:billing_bot"]["mode"] == "allow"
    assert tools["agent.handoff:billing_bot"]["mode"] == "allow"


def test_mcp_block_lowers_to_a_tool():
    # mcp is sugar for tools — a mcp grant resolves to an ordinary tool key.
    doc = "mcp: { knowledge: { mode: approval_required } }"
    tools = _eff(resolve_text(doc))["default"]["tools"]
    assert tools["knowledge"]["mode"] == "approval_required"


def test_per_agent_boundary_caps_a_role_grant():
    # A boundary declared in an agent body is a ceiling for that agent only; it
    # intersects the grant (AND) like any ceiling.
    doc = """
    agents:
      bot:
        boundary: { tools: { refund: { constraint: "args.amount <= 100" } } }
        roles:
          support: { tools: { refund: { mode: allow } } }
    """
    refund = _eff(resolve_text(doc, agent="bot"))["support"]["tools"]["refund"]
    assert refund["mode"] == "allow"
    assert any("args.amount <= 100" in c for c in refund["constraints"])


def test_resolve_file_from_path(tmp_path: Path):
    p = tmp_path / "policy.yaml"
    p.write_text("tools: { a: { mode: allow } }\n", encoding="utf-8")
    res = resolve_file(p)
    assert _eff(res)["default"]["tools"]["a"]["mode"] == "allow"


def test_reach_denied_when_boundary_omits_it_even_if_granted():
    # closed-world / confused-deputy guard: a reach the ceiling never lists is
    # denied even though a capability grants it.
    doc = """
    boundary:
      reach: { billing_bot: { as: tool } }
    agents:
      bot:
        roles:
          support:
            reach:
              billing_bot: { as: tool }
              evil_bot: { as: tool }
    """
    tools = _eff(resolve_text(doc, agent="bot"))["support"]["tools"]
    assert tools["agent.tool:billing_bot"]["mode"] == "allow"
    assert tools["agent.tool:evil_bot"]["mode"] == "deny"


# --- imports -------------------------------------------------------------


def _loader(files: dict[str, str]):
    """An in-memory loader: project-relative path -> file text (missing → OSError)."""

    def load(path: str) -> str:
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    return load


def test_import_without_loader_errors():
    with pytest.raises(LinkError, match="base path"):
        resolve_text("import: [ other.yaml ]")


def test_export_block_is_accepted_and_inert_for_own_resolution():
    # A file may declare exports (for other importers); resolving it directly is
    # fine and its exports don't affect its own effective policy.
    res = resolve_text(
        "tools: { a: { mode: allow } }\nexport: { frag: { tools: { b: {} } } }"
    )
    assert set(_eff(res)["default"]["tools"]) == {"a"}


def test_import_whole_file_at_top_applies_to_all():
    loader = _loader({"base.yaml": "tools: { read_ticket: { mode: allow } }\n"})
    res = resolve_text("import: [ base.yaml ]", loader=loader)
    assert "read_ticket" in _eff(res)["default"]["tools"]


def test_import_named_export_is_scoped_by_position():
    # A fragment imported in a role body applies only to that cell (position=scope).
    loader = _loader(
        {"caps.yaml": "export:\n  refunds:\n    tools: { refund: { mode: allow } }\n"}
    )
    doc = """
    agents:
      bot:
        roles:
          support:
            import: [ caps.yaml#refunds ]
          billing: {}
    """
    eff = _eff(resolve_text(doc, agent="bot", loader=loader))
    assert "refund" in eff["support"]["tools"]
    assert "refund" not in eff["billing"]["tools"]  # not imported here
    assert "refund" not in eff["default"]["tools"]


def test_import_is_transitive():
    loader = _loader(
        {
            "a.yaml": "import: [ b.yaml ]\ntools: { from_a: { mode: allow } }\n",
            "b.yaml": "tools: { from_b: { mode: allow } }\n",
        }
    )
    tools = _eff(resolve_text("import: [ a.yaml ]", loader=loader))["default"]["tools"]
    assert {"from_a", "from_b"} <= set(tools)


def test_import_cycle_is_a_linkerror():
    loader = _loader(
        {"a.yaml": "import: [ b.yaml ]\n", "b.yaml": "import: [ a.yaml ]\n"}
    )
    with pytest.raises(LinkError, match="cycle"):
        resolve_text("import: [ a.yaml ]", loader=loader)


def test_import_missing_export_is_a_linkerror():
    loader = _loader({"caps.yaml": "export: { other: { tools: {} } }\n"})
    with pytest.raises(LinkError, match="no export named"):
        resolve_text("import: [ caps.yaml#refunds ]", loader=loader)


def test_import_of_structural_file_rejected():
    # An imported file must be leaf-only; importing one that declares agents:/roles:
    # is a later increment.
    loader = _loader(
        {"full.yaml": "agents: { bot: { tools: { a: { mode: allow } } } }\n"}
    )
    with pytest.raises(LinkError, match="leaf-only"):
        resolve_text("import: [ full.yaml ]", loader=loader)


def test_import_matches_inlining_golden():
    # Importing a fragment resolves identically to inlining the same grants.
    loader = _loader(
        {"caps.yaml": "export:\n  c:\n    tools: { refund: { mode: allow } }\n"}
    )
    imported = resolve_text("import: [ caps.yaml#c ]", loader=loader)
    inlined = resolve_text("tools: { refund: { mode: allow } }")
    assert json.dumps(_eff(imported), sort_keys=True) == json.dumps(
        _eff(inlined), sort_keys=True
    )


def test_resolve_file_imports_relative_to_entry_dir(tmp_path: Path):
    (tmp_path / "caps.yaml").write_text(
        "export:\n  refunds:\n    tools: { refund: { mode: allow } }\n",
        encoding="utf-8",
    )
    (tmp_path / "policy.yaml").write_text(
        "import: [ caps.yaml#refunds ]\n", encoding="utf-8"
    )
    res = resolve_file(tmp_path / "policy.yaml")
    assert "refund" in _eff(res)["default"]["tools"]


def test_transitive_import_resolves_relative_to_importing_file(tmp_path: Path):
    # A nested file's own import resolves against ITS dir, not the entry's:
    # shared/caps.yaml imports "helper.yaml" meaning shared/helper.yaml.
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "helper.yaml").write_text(
        "tools: { helped: { mode: allow } }\n", encoding="utf-8"
    )
    (tmp_path / "shared" / "caps.yaml").write_text(
        "import: [ helper.yaml ]\ntools: { capped: { mode: allow } }\n",
        encoding="utf-8",
    )
    (tmp_path / "policy.yaml").write_text(
        "import: [ shared/caps.yaml ]\n", encoding="utf-8"
    )
    tools = _eff(resolve_file(tmp_path / "policy.yaml"))["default"]["tools"]
    assert {"helped", "capped"} <= set(tools)


def test_absolute_import_path_rejected():
    with pytest.raises(LinkError, match="absolute import"):
        resolve_text("import: [ /etc/passwd ]", loader=_loader({}))


def test_escaping_import_path_rejected():
    with pytest.raises(LinkError, match="escapes the project"):
        resolve_text("import: [ ../secret.yaml ]", loader=_loader({}))


def test_missing_import_file_is_a_linkerror():
    with pytest.raises(LinkError, match="cannot import"):
        resolve_text("import: [ nope.yaml ]", loader=_loader({}))


def test_per_agent_import_isolation():
    # 'other' has a broken import, but resolving the valid 'bot' must succeed —
    # only the target agent's scopes are walked.
    loader = _loader({"caps.yaml": "tools: { a: { mode: allow } }\n"})
    doc = """
    agents:
      bot:
        roles:
          support: { import: [ caps.yaml ] }
      other:
        roles:
          x: { import: [ missing.yaml ] }
    """
    eff = _eff(resolve_text(doc, agent="bot", loader=loader))
    assert "a" in eff["support"]["tools"]


def test_plural_import_key_rejected():
    # `imports:` (the field name) is not the `import:` alias — reject it.
    with pytest.raises(LinkError):
        parse_entry("imports: [ caps.yaml ]")


def test_via_field_name_rejected():
    # `via:` is the field name; the alias is `as:` — reject the field-name spelling.
    with pytest.raises(LinkError):
        parse_entry("reach: { bot: { via: [tool] } }")


def test_imported_file_parsed_once_across_scopes():
    calls = {"n": 0}

    def counting_loader(path: str) -> str:
        calls["n"] += 1
        return "tools: { a: { mode: allow } }\n"

    doc = """
    import: [ caps.yaml ]
    agents:
      bot:
        import: [ caps.yaml ]
        roles:
          support: { import: [ caps.yaml ] }
    """
    resolve_text(doc, agent="bot", loader=counting_loader)
    assert calls["n"] == 1  # cached — one read despite three import sites


def test_imported_grant_provenance_names_the_source_file():
    # An imported grant's ModuleContent source names the file it came from, not
    # the entry — so signing/audit/debug attribute it correctly.
    loader = _loader(
        {"caps.yaml": "export:\n  c:\n    tools: { refund: { mode: allow } }\n"}
    )
    res = resolve_text("import: [ caps.yaml#c ]", loader=loader)
    provs = res.by_role["default"].trace.contributors["refund"]
    assert any("caps.yaml" in p.source for p in provs)


def test_imported_fragment_with_boundary_rejected():
    # Imports grant only; an imported ceiling would intersect and can silently
    # deny every grant, so a boundary in an import is rejected.
    loader = _loader({"ceil.yaml": "boundary: { tools: { x: {} } }\n"})
    with pytest.raises(LinkError, match="may only grant"):
        resolve_text("import: [ ceil.yaml ]", loader=loader)


def test_duplicate_import_refs_are_deduped():
    loader = _loader({"caps.yaml": "tools: { a: { mode: allow } }\n"})
    res = resolve_text("import: [ caps.yaml, caps.yaml ]", loader=loader)
    provs = res.by_role["default"].trace.contributors["a"]
    assert len(provs) == 1  # one contribution despite the repeated ref


def test_symlink_escaping_project_is_rejected(tmp_path: Path):
    import os

    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "secret.yaml").write_text(
        "tools: { leaked: { mode: allow } }\n", encoding="utf-8"
    )
    try:
        os.symlink(tmp_path / "secret.yaml", proj / "link.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")
    (proj / "policy.yaml").write_text("import: [ link.yaml ]\n", encoding="utf-8")
    with pytest.raises(LinkError, match="escapes the project"):
        resolve_file(proj / "policy.yaml")


def test_import_of_the_entry_file_is_a_cycle(tmp_path: Path):
    (tmp_path / "a.yaml").write_text("import: [ policy.yaml ]\n", encoding="utf-8")
    (tmp_path / "policy.yaml").write_text("import: [ a.yaml ]\n", encoding="utf-8")
    with pytest.raises(LinkError, match="cycle"):
        resolve_file(tmp_path / "policy.yaml")


def test_diamond_import_contributes_shared_fragment_once():
    # x → a, b; a → c, b → c. c is reached via two paths but contributes once.
    loader = _loader(
        {
            "x.yaml": "import: [ a.yaml, b.yaml ]\n",
            "a.yaml": "import: [ c.yaml ]\n",
            "b.yaml": "import: [ c.yaml ]\n",
            "c.yaml": "tools: { shared: { mode: allow } }\n",
        }
    )
    res = resolve_text("import: [ x.yaml ]", loader=loader)
    provs = res.by_role["default"].trace.contributors["shared"]
    assert len(provs) == 1


def test_same_target_via_different_ref_strings_deduped():
    # `caps.yaml` and `./caps.yaml` resolve to the same file → one contribution.
    loader = _loader({"caps.yaml": "tools: { a: { mode: allow } }\n"})
    res = resolve_text("import: [ caps.yaml, ./caps.yaml ]", loader=loader)
    assert len(res.by_role["default"].trace.contributors["a"]) == 1


def test_local_grant_provenance_names_the_entry_file(tmp_path: Path):
    # A file's own (non-imported) grants carry the entry file as source, not a
    # hardcoded "policy.yaml".
    p = tmp_path / "main.yaml"
    p.write_text("tools: { a: { mode: allow } }\n", encoding="utf-8")
    provs = resolve_file(p).by_role["default"].trace.contributors["a"]
    assert all(str(p) in pr.source for pr in provs)


# --- validation errors surface as source-named LinkError --------------------


def test_bad_constraint_is_a_source_named_linkerror():
    # A malformed constraint fails when lower() builds the SDK tool policy; it must
    # come back as a LinkError naming the source, not a raw pydantic error.
    with pytest.raises(LinkError, match="policy.yaml"):
        resolve_text('tools: { x: { constraint: "args.amount <<< 5" } }')


def test_reserved_agent_tool_key_is_a_linkerror():
    # agent.run in a tools block trips #124's reserved-name guard in lower(); the
    # resolve wrapper turns that into a LinkError rather than a ValidationError.
    with pytest.raises(LinkError):
        resolve_text('tools: { "agent.run": { mode: allow } }')


def test_tools_mcp_same_key_rejected():
    with pytest.raises(LinkError, match="both 'tools' and 'mcp'"):
        resolve_text("tools: { x: { mode: allow } }\nmcp: { x: { mode: allow } }")


def test_constraint_and_constraints_both_rejected():
    # Giving both forms is ambiguous and would silently drop one (widening access),
    # so it is a parse error rather than a quiet merge.
    with pytest.raises(LinkError, match="not both"):
        resolve_text(
            'tools: { x: { constraint: "args.a <= 1", constraints: ["args.b <= 2"] } }'
        )


# --- golden parity vs the tier-folder layout -----------------------------


def _mod(name, kind, tools, *, default_mode="allow"):
    return ModuleContent(
        name=name,
        kind=kind,
        policy=AgentPolicy(
            default_policy=BaseToolPolicy(mode=default_mode), tools=tools
        ),
        source=f"{name}.yaml",
        content_hash=f"hash-{name}",
    )


def test_golden_parity_all_compose_ceiling_boundary():
    # A compose policy and the equivalent tier-folder project (one ceiling
    # boundary + one capability, no roles → the single default role) must resolve
    # to byte-identical effective policy — proving the fold path is reused.
    compose = resolve_text(
        """
        boundary:
          tools:
            refund_order: { constraint: "args.amount <= 1000" }
            view_orders: {}
        tools:
          view_orders: { mode: allow }
          refund_order: { mode: allow, constraint: 'args.currency == "USD"' }
        """
    )
    ceiling = _mod(
        "b",
        "boundary",
        {
            "refund_order": BaseToolPolicy(
                mode="allow", constraints=["args.amount <= 1000"]
            ),
            "view_orders": BaseToolPolicy(mode="allow"),
        },
        default_mode="deny",
    )
    cap = _mod(
        "c",
        "capability",
        {
            "view_orders": BaseToolPolicy(mode="allow"),
            "refund_order": BaseToolPolicy(
                mode="allow", constraints=['args.currency == "USD"']
            ),
        },
    )
    tier = resolve_for_project([ceiling], [cap], None)
    assert json.dumps(_eff(compose), sort_keys=True) == json.dumps(
        _eff(tier), sort_keys=True
    )


def test_capability_deny_style_rules_still_rejected_via_boundary_semantics():
    # Grants may only allow/approve; the grammar has no deny in tools/reach, so a
    # "deny" is expressed as a boundary omission (closed-world). A tool granted
    # nowhere and not in the boundary is simply absent from the effective policy.
    res = resolve_text(
        """
        boundary:
          tools: { a: {} }
        tools: { a: { mode: allow }, b: { mode: allow } }
        """
    )
    tools = _eff(res)["default"]["tools"]
    assert tools["a"]["mode"] == "allow"
    assert "b" not in tools  # b not in the ceiling → shadowed away
