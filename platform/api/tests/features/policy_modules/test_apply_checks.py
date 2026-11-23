"""run_checks: the ordered checks a set of draft policy files must pass."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from hexgate.security import PolicyLint

from hexgate_api.features.agents import service as agents_service
from hexgate_api.features.policy_modules import service
from hexgate_api.features.policy_modules.apply_checks import (
    MAX_ITEMS,
    Check,
    ChecksResult,
    _defect,
    _strip_roles,
    run_checks,
)
from hexgate_api.schemas import AgentManifest, PolicyFileDraft

PID = "prj_checks"
ORDER = ["yaml", "composes", "lints", "dry_runs", "boundary"]

_ENTRY = """\
boundary:
  tools:
    refund: { mode: allow, constraint: 'args.amount <= 100' }
    view: { mode: allow }
agents:
  bot:
    roles:
      support: { import: [ grants.yaml ] }
"""
_GRANTS = "tools:\n  refund: { mode: allow, constraint: 'ctx.tier == \"gold\"' }\n"
# The entry with a guard bot's manifest does not declare: an error lint.
_ENTRY_GUARD_TYPO = _ENTRY.replace(
    "  bot:\n", "  bot:\n    guards: { secret_redacter: { enabled: false } }\n"
)


@dataclass
class Sample:
    role: str
    tool: str
    expect: str
    agent: str = "bot"
    args: dict = field(default_factory=dict)
    attributes: dict = field(default_factory=dict)


GOLD_REFUND = Sample(
    "support", "refund", "allow", args={"amount": 5}, attributes={"tier": "gold"}
)


async def _store(session, files: dict[str, str]) -> None:
    for name, content in files.items():
        await service.upsert_file(
            session, project_id=PID, name=name, content=content, actor_user_id="u"
        )


@pytest.fixture
async def stored(session):
    """The project's saved files: a boundary and a ctx-gated refund grant."""
    await _store(session, {"policy.yaml": _ENTRY, "grants.yaml": _GRANTS})


@pytest.fixture
async def guarded_bot(session, monkeypatch):
    """``bot`` registered with its two tools and one guard, ``secret_redactor``."""
    monkeypatch.setattr(agents_service, "compile_bundle", lambda *_, **__: None)
    manifest = AgentManifest.model_validate(
        {
            "name": "bot",
            "framework": "hexgate",
            "tools": [
                {
                    "name": t,
                    "description": t,
                    "input_schema": {"properties": {}, "required": []},
                }
                for t in ("refund", "view")
            ],
            "guards": [
                {"name": "secret_redactor", "position": "before", "kind": "official"}
            ],
        }
    )
    await agents_service.register_manifest(
        session, PID, manifest, sign=lambda _: b"sig"
    )


def _drafts(files: dict[str, str]) -> list[PolicyFileDraft]:
    return [PolicyFileDraft(name=n, content=c) for n, c in files.items()]


def _statuses(result) -> list[str]:
    return [c.status for c in result.checks]


def _by_id(result, check_id: str):
    [check] = [c for c in result.checks if c.id == check_id]
    return check


async def _run(session, files: dict[str, str], samples=(GOLD_REFUND,)):
    return await run_checks(session, PID, _drafts(files), list(samples))


@pytest.mark.usefixtures("stored")
async def test_run_checks_happy_path(session) -> None:
    result = await _run(session, {"grants.yaml": _GRANTS + "  view: { mode: allow }\n"})
    assert [c.id for c in result.checks] == ORDER
    assert _statuses(result) == ["pass"] * 5
    assert result.passed
    assert result.boundary_changes == []


@pytest.mark.usefixtures("stored")
async def test_when_a_file_does_not_parse_then_yaml_fails_and_the_rest_are_skipped(
    session,
) -> None:
    result = await _run(session, {"grants.yaml": "tools: [", "other.yaml": "x: 1\n"})
    assert _statuses(result) == ["fail"] + ["skipped"] * 4
    assert not result.passed
    items = _by_id(result, "yaml").items
    assert [i["file"] for i in items] == ["grants.yaml", "other.yaml"]
    assert items[0]["error"].startswith("grants.yaml: invalid YAML")
    assert _by_id(result, "boundary").detail == "yaml failed"


@pytest.mark.usefixtures("stored")
async def test_when_the_project_does_not_resolve_then_composes_fails(session) -> None:
    entry = _ENTRY.replace("grants.yaml", "missing.yaml")
    result = await _run(session, {"policy.yaml": entry})
    assert _statuses(result) == ["pass", "fail", "skipped", "skipped", "skipped"]
    assert "missing.yaml" in _by_id(result, "composes").items[0]["error"]
    assert not result.passed


@pytest.mark.usefixtures("stored")
async def test_when_only_a_warning_lint_is_found_then_lints_warns_and_the_run_passes(
    session,
) -> None:
    grants = "tools:\n  refund: { mode: allow, constraint: 'foo.x == 1' }\n"
    result = await _run(session, {"grants.yaml": grants}, samples=())
    assert _statuses(result) == ["pass", "pass", "warn", "pass", "pass"]
    assert result.passed
    [lint] = _by_id(result, "lints").items
    assert (lint["code"], lint["severity"]) == ("unknown-root", "warning")
    assert lint["message"].startswith("agent 'bot': ")
    assert _by_id(result, "dry_runs").detail == "no samples"


@pytest.mark.usefixtures("stored", "guarded_bot")
async def test_when_the_draft_introduces_an_error_lint_then_lints_fails(
    session,
) -> None:
    result = await _run(session, {"policy.yaml": _ENTRY_GUARD_TYPO})
    assert _statuses(result) == ["pass", "pass", "fail", "skipped", "skipped"]
    check = _by_id(result, "lints")
    assert check.detail == "1 new error lint(s)"
    new = [i["code"] for i in check.items if i.get("new")]
    assert new == ["unknown-guard"]
    assert all(i["severity"] == "error" for i in check.items if i.get("new"))
    assert not result.passed


@pytest.mark.usefixtures("guarded_bot")
async def test_when_the_saved_files_already_have_the_error_lint_then_lints_warns(
    session,
) -> None:
    await _store(session, {"policy.yaml": _ENTRY_GUARD_TYPO, "grants.yaml": _GRANTS})
    result = await _run(session, {"grants.yaml": _GRANTS + "  view: { mode: allow }\n"})
    assert _statuses(result) == ["pass", "pass", "warn", "pass", "pass"]
    items = _by_id(result, "lints").items
    assert [i["code"] for i in items if i["severity"] == "error"] == ["unknown-guard"]
    assert not any(i.get("new") for i in items)
    assert result.passed


@pytest.mark.usefixtures("stored")
async def test_when_the_draft_introduces_a_link_error_then_lints_fails(
    session, monkeypatch
) -> None:
    # An analyzer link-error on a project that resolves (a column of the role
    # map that does not link), which no real fixture here produces.
    warning = PolicyLint("unknown-root", "warning", "a warning")
    link_error = PolicyLint("link-error", "error", "column does not link")

    async def compose_preview(*_args, **_kwargs):  # the drafted files
        return {}, {}, [warning, link_error]

    async def check_auto(*_args, **_kwargs):  # the saved files
        return [warning]

    monkeypatch.setattr(service, "compose_preview", compose_preview)
    monkeypatch.setattr(service, "check_auto", check_auto)
    result = await _run(session, {"grants.yaml": _GRANTS})
    assert _statuses(result) == ["pass", "pass", "fail", "skipped", "skipped"]
    items = _by_id(result, "lints").items
    assert [(i["code"], i.get("new")) for i in items] == [
        ("link-error", True),
        ("unknown-root", None),
    ]


@pytest.mark.usefixtures("stored")
async def test_when_a_sample_carries_attributes_then_the_ctx_rule_reads_them(
    session,
) -> None:
    silver = Sample(
        "support", "refund", "deny", args={"amount": 5}, attributes={"tier": "silver"}
    )
    result = await _run(session, {"grants.yaml": _GRANTS}, [GOLD_REFUND, silver])
    check = _by_id(result, "dry_runs")
    assert (check.status, check.detail) == ("pass", "2 sample(s) as expected")
    assert result.passed


@pytest.mark.usefixtures("stored")
async def test_when_an_outcome_differs_from_expect_then_dry_runs_fails(
    session,
) -> None:
    # Without the attribute the ctx rule denies, so "allow" is not met.
    no_tier = Sample("support", "refund", "allow", args={"amount": 5})
    result = await _run(session, {"grants.yaml": _GRANTS}, [GOLD_REFUND, no_tier])
    assert _statuses(result) == ["pass", "pass", "pass", "fail", "skipped"]
    [item] = _by_id(result, "dry_runs").items
    assert item["index"] == 1
    assert (item["expect"], item["outcome"]) == ("allow", "deny")
    assert "ctx.tier" in item["reason"]
    assert result.boundary_changes == []


@pytest.mark.usefixtures("stored")
async def test_when_a_sample_names_an_unknown_role_then_dry_runs_fails(
    session,
) -> None:
    samples = [
        Sample("nobody", "refund", "deny"),
        Sample("support", "refund", "deny", agent="ghost"),
    ]
    result = await _run(session, {"grants.yaml": _GRANTS}, samples)
    check = _by_id(result, "dry_runs")
    assert check.status == "fail"
    assert [i["outcome"] for i in check.items] == [None, None]
    assert check.items[0]["reason"] == "role 'nobody' not defined"
    assert "agent 'ghost' is not declared" in check.items[1]["reason"]


@pytest.mark.usefixtures("stored")
async def test_when_a_boundary_is_widened_with_every_other_check_green_then_not_saveable(
    session,
) -> None:
    entry = _ENTRY.replace("args.amount <= 100", "args.amount <= 5000")
    result = await _run(session, {"policy.yaml": entry})
    assert _statuses(result) == ["pass", "pass", "pass", "pass", "fail"]
    assert not result.passed
    [change] = result.boundary_changes
    assert (change.path, change.kind) == ("boundary.tools.refund", "widened")


@pytest.mark.usefixtures("stored")
async def test_when_a_boundary_is_only_tightened_then_boundary_passes(session) -> None:
    entry = _ENTRY.replace("args.amount <= 100", "args.amount <= 50")
    result = await _run(session, {"policy.yaml": entry})
    assert _by_id(result, "boundary").status == "pass"
    assert result.passed
    assert [c.kind for c in result.boundary_changes] == ["tightened"]


async def test_when_the_draft_adds_the_first_policy_yaml_then_boundary_fails(
    session,
) -> None:
    result = await _run(session, {"policy.yaml": _ENTRY, "grants.yaml": _GRANTS})
    assert _statuses(result) == ["pass", "pass", "pass", "pass", "fail"]
    assert "no 'policy.yaml'" in _by_id(result, "boundary").detail
    assert result.boundary_changes == []


@pytest.mark.usefixtures("stored")
async def test_when_a_boundary_block_does_not_lower_then_boundary_fails(
    session,
) -> None:
    # Not imported, so the project still composes; the comparison still lowers it.
    loose = "boundary:\n  tools:\n    agent.run: { mode: allow }\n"
    result = await _run(session, {"loose.yaml": loose})
    assert _statuses(result) == ["pass", "pass", "pass", "pass", "fail"]
    assert "loose.yaml" in _by_id(result, "boundary").detail
    assert not result.passed


@pytest.mark.usefixtures("stored")
async def test_when_a_check_finds_more_than_the_cap_then_the_detail_counts_the_rest(
    session,
) -> None:
    check = _by_id(await _run(session, {f"f{i}.yaml": "[" for i in range(53)}), "yaml")
    assert len(check.items) == MAX_ITEMS
    assert check.detail == "53 file(s) do not parse; 3 more not listed"


def test_when_a_check_was_skipped_then_the_result_has_not_passed() -> None:
    skipped = Check(id="boundary", status="skipped", detail="dry_runs failed")
    warned = Check(id="lints", status="warn", detail="1 lint(s)")
    assert ChecksResult(checks=[warned], boundary_changes=[]).passed
    assert not ChecksResult(checks=[warned, skipped], boundary_changes=[]).passed


# A negated condition on a path no call sets: an error lint in every role that
# imports it, reported once, naming those roles unless every role carries it.
_BAD = "tools:\n  refund: { mode: allow, constraint: 'not (foo.bar == 1)' }\n"
_OK = "tools:\n  view: { mode: allow }\n"


def _bot(**roles: str) -> str:
    """An entry whose agent ``bot`` has these roles, each importing a file."""
    body = "".join(f"      {r}: {{ import: [ {f} ] }}\n" for r, f in roles.items())
    return f"agents:\n  bot:\n    roles:\n{body}"


async def _store_bot(session, entry: str) -> None:
    await _store(session, {"policy.yaml": entry, "bad.yaml": _BAD, "ok.yaml": _OK})


async def _lints_of(session, entry: str):
    return _by_id(await _run(session, {"policy.yaml": entry}, samples=()), "lints")


async def _lints_of_files(session, files: dict[str, str]):
    return _by_id(await _run(session, files, samples=()), "lints")


async def test_when_an_error_is_only_on_the_draft_then_lints_fails(session) -> None:
    await _store_bot(session, _bot(default="ok.yaml", support="ok.yaml"))
    check = await _lints_of(session, _bot(default="ok.yaml", support="bad.yaml"))
    assert check.status == "fail"
    new = [i["code"] for i in check.items if i.get("new")]
    assert new == ["unknown-root"]


@pytest.mark.parametrize(
    "drafted",
    [
        # the same defect, now reported in another role
        _bot(default="ok.yaml", support="ok.yaml", sales="bad.yaml"),
        # spread to every role, so reported with no role clause
        _bot(default="bad.yaml", support="bad.yaml", sales="bad.yaml"),
    ],
)
async def test_when_a_saved_error_is_reported_in_other_roles_then_lints_warns(
    session, drafted: str
) -> None:
    await _store_bot(
        session, _bot(default="ok.yaml", support="bad.yaml", sales="ok.yaml")
    )
    check = await _lints_of(session, drafted)
    assert check.status == "warn"
    errors = [i["code"] for i in check.items if i["severity"] == "error"]
    assert errors == ["unknown-root"]
    assert not any(i.get("new") for i in check.items)


async def test_when_the_draft_deletes_a_role_that_never_carried_a_saved_error_then_lints_warns(
    session,
) -> None:
    # Saved: "in role 'default', role 'support'"; the draft: no role clause.
    await _store_bot(
        session, _bot(default="bad.yaml", support="bad.yaml", sales="ok.yaml")
    )
    check = await _lints_of(session, _bot(default="bad.yaml", support="bad.yaml"))
    assert check.status == "warn"


async def test_when_the_draft_keeps_a_saved_error_only_in_an_agent_scope_then_lints_warns(
    session,
) -> None:
    # Saved: reported once, by the "*" view; the draft: reported by bot only.
    agent = "agents:\n  bot:\n    roles:\n      support: { import: [ bad.yaml ] }\n"
    await _store_bot(session, "import: [ bad.yaml ]\n" + agent)
    check = await _lints_of(session, "import: [ ok.yaml ]\n" + agent)
    assert check.status == "warn"
    assert {i.get("agent") for i in check.items if i["severity"] == "error"} == {"bot"}


_USAGE_DENY = "usage: { on_unavailable: deny }\n"
_NEGATED_CAP = "tools:\n  refund: { mode: allow, constraint: 'not (agent_usage.denials_1h < 3)' }\n"


async def _analyzer_errors(session, header: str, bad: str, **roles: str) -> list:
    """The error lints the lints check reads for ``bot`` with these roles."""
    files = {"policy.yaml": header + _bot(**roles), "bad.yaml": bad, "ok.yaml": _OK}
    lints = await service.check_auto(session, PID, _drafts(files))
    return [lint for lint in lints if lint.severity == "error"]


@pytest.mark.parametrize(
    ("header", "bad", "code"),
    [
        ("", _BAD, "unknown-root"),  # analyzer.py:1373
        (_USAGE_DENY, _NEGATED_CAP, "negated-usage-cap"),  # analyzer.py:1460
    ],
)
@pytest.mark.parametrize(
    "carriers",
    [("support",), ("support", "sales")],
    ids=["one role", "several roles"],
)
async def test_strip_roles_happy_path(session, header, bad, code, carriers) -> None:
    roles = {
        r: "bad.yaml" if r in carriers else "ok.yaml"
        for r in ("default", "support", "sales")
    }
    [some] = await _analyzer_errors(session, header, bad, **roles)
    every_role = dict.fromkeys(("default", "support", "sales"), "bad.yaml")
    [every] = await _analyzer_errors(session, header, bad, **every_role)
    assert some.code == every.code == code
    assert some.message != every.message  # the clause is there to strip
    assert _strip_roles(some.message) == every.message
    assert _defect(some) == _defect(every)


# A negated condition on an argument the tool does not take: an error
# `unknown-arg` per role that imports it, its message opening with the role.
_BAD_ARG = "tools:\n  refund: { mode: allow, constraint: 'not (args.amout == 1)' }\n"


@pytest.fixture
async def refund_bot(session, monkeypatch):
    """``bot`` registered with ``refund(amount)`` and ``view``."""
    monkeypatch.setattr(agents_service, "compile_bundle", lambda *_, **__: None)
    schema = {"properties": {"amount": {"title": "Amount", "type": "number"}}}
    tools = [
        {
            "name": "refund",
            "description": "r",
            "input_schema": {**schema, "required": []},
        },
        {
            "name": "view",
            "description": "v",
            "input_schema": {"properties": {}, "required": []},
        },
    ]
    manifest = AgentManifest.model_validate(
        {"name": "bot", "framework": "hexgate", "tools": tools}
    )
    await agents_service.register_manifest(
        session, PID, manifest, sign=lambda _: b"sig"
    )


async def _store_args(session, entry: str) -> None:
    await _store(session, {"policy.yaml": entry, "bad.yaml": _BAD_ARG, "ok.yaml": _OK})


@pytest.mark.usefixtures("refund_bot")
async def test_when_a_lint_opens_with_its_role_then_strip_roles_drops_it(
    session,
) -> None:
    [support] = await _analyzer_errors(
        session, "", _BAD_ARG, default="ok.yaml", support="bad.yaml", sales="ok.yaml"
    )
    [sales] = await _analyzer_errors(
        session, "", _BAD_ARG, default="ok.yaml", support="ok.yaml", sales="bad.yaml"
    )
    assert support.code == sales.code == "unknown-arg"
    assert support.message.startswith("role 'support' constrains 'refund'")
    assert _strip_roles(support.message) == _strip_roles(sales.message)
    assert _defect(support) == _defect(sales)


@pytest.mark.usefixtures("refund_bot")
@pytest.mark.parametrize(
    "drafted",
    [
        # copied to a new role
        _bot(default="ok.yaml", support="bad.yaml", sales="bad.yaml"),
        # the role carrying it renamed
        _bot(default="ok.yaml", helpdesk="bad.yaml", sales="ok.yaml"),
    ],
    ids=["copied to a role", "role renamed"],
)
async def test_when_a_saved_unknown_arg_error_moves_to_another_role_then_lints_warns(
    session, drafted: str
) -> None:
    await _store_args(
        session, _bot(default="ok.yaml", support="bad.yaml", sales="ok.yaml")
    )
    check = await _lints_of(session, drafted)
    assert check.status == "warn"
    assert "unknown-arg" in {i["code"] for i in check.items if i["severity"] == "error"}
    assert not any(i.get("new") for i in check.items)


@pytest.mark.usefixtures("refund_bot")
async def test_when_an_unknown_arg_error_is_only_on_the_draft_then_lints_fails(
    session,
) -> None:
    await _store_args(session, _bot(default="ok.yaml", support="ok.yaml"))
    check = await _lints_of(session, _bot(default="ok.yaml", support="bad.yaml"))
    assert check.status == "fail"
    assert [i["code"] for i in check.items if i.get("new")] == ["unknown-arg"]


@pytest.mark.usefixtures("refund_bot")
async def test_when_another_role_gets_a_different_unknown_arg_then_lints_fails(
    session,
) -> None:
    other = "tools:\n  refund: { mode: allow, constraint: 'not (args.amuont == 1)' }\n"
    await _store(
        session,
        {
            "policy.yaml": _bot(default="ok.yaml", support="bad.yaml", sales="ok.yaml"),
            "bad.yaml": _BAD_ARG,
            "other.yaml": other,
            "ok.yaml": _OK,
        },
    )
    drafted = _bot(default="ok.yaml", support="bad.yaml", sales="other.yaml")
    check = await _lints_of(session, drafted)
    assert check.status == "fail"
    [new] = [i for i in check.items if i.get("new")]
    assert "args.amuont" in new["message"]


@pytest.mark.parametrize(
    "content",
    [
        "consts:\n  cutoff: 2026-02-30\n",  # a YAML date that does not exist
        "tools:\n  refund: { mode: allow, constraint: 2001-13-45 }\n",
        "[" * 5000,  # nested past the interpreter's stack
        'consts:\n  a: !!timestamp "abc"\n',  # AttributeError in the loader
        'consts:\n  a: !!bool "abc"\n',  # KeyError
        'consts:\n  a: !!int ""\n',  # IndexError
    ],
    ids=["date const", "date constraint", "deep nesting", "timestamp", "bool", "int"],
)
@pytest.mark.usefixtures("stored")
async def test_when_a_draft_breaks_the_yaml_loader_then_yaml_fails(
    session, content: str
) -> None:
    result = await _run(session, {"grants.yaml": content})
    assert _statuses(result) == ["fail"] + ["skipped"] * 4
    [item] = _by_id(result, "yaml").items
    assert item["error"].startswith("grants.yaml: ")


@pytest.mark.usefixtures("stored")
async def test_when_a_sample_names_an_undeclared_agent_then_dry_runs_fails(
    session,
) -> None:
    # "default" exists in the generic view, so the call would evaluate as "*".
    ghost = Sample("default", "refund", "deny", agent="ghost")
    result = await _run(session, {"grants.yaml": _GRANTS}, [ghost])
    [item] = _by_id(result, "dry_runs").items
    assert item["outcome"] is None
    assert item["reason"].startswith("agent 'ghost' is not declared")


_DEPT = "tools:\n  refund: {{ mode: allow, constraint: '{}' }}\n"


async def test_when_a_saved_warning_becomes_an_error_then_lints_fails(session) -> None:
    # One path read plainly (a warning), then under `not` (an error).
    await _store(
        session,
        {
            "policy.yaml": _bot(support="grants.yaml"),
            "grants.yaml": _DEPT.format('user.department == "x"'),
        },
    )
    result = await _run(
        session, {"grants.yaml": _DEPT.format('not (user.department == "x")')}, ()
    )
    check = _by_id(result, "lints")
    assert check.status == "fail"
    assert [(i["code"], i["severity"]) for i in check.items if i.get("new")] == [
        ("unknown-root", "error")
    ]


async def test_when_an_agent_errs_where_the_generic_view_warns_then_lints_fails(
    session,
) -> None:
    # The "*" view warns on user.department; bot's fail-open grant makes the
    # same text an error, which must not be deduped away as the "*" lint.
    top = (
        "tools:\n  view: { mode: allow, constraint: 'user.department == \"sales\"' }\n"
    )
    entry = "import: [ top.yaml ]\n" + _bot(support="grants.yaml")
    await _store(session, {"policy.yaml": entry, "top.yaml": top, "grants.yaml": _OK})
    drafted = _DEPT.format('not (user.department == "contractor")')
    result = await _run(session, {"grants.yaml": drafted}, ())
    check = _by_id(result, "lints")
    assert check.status == "fail"
    [new] = [i for i in check.items if i.get("new")]
    assert (new["code"], new["severity"], new["agent"]) == (
        "unknown-root",
        "error",
        "bot",
    )


async def test_known_limit_when_a_new_rule_repeats_a_saved_dead_path_then_lints_only_warns(
    session,
) -> None:
    """Known limit, pinned so its fix shows up. ``unknown-root`` carries no
    tool and the compose source is always ``policy.yaml``, so a new rule on
    another tool reading the same unset path is the saved defect: ``warn``
    today. This flips to ``fail`` when the SDK puts the tool (and location)
    into these lints (a follow-up SDK change); that change updates this test."""
    saved = "tools:\n  view: { mode: allow, constraint: 'not (user.department == \"contractor\")' }\n"
    drafted = saved + (
        "  delete_account: { mode: allow, constraint: 'not (user.department == \"intern\")' }\n"
    )
    await _store(
        session, {"policy.yaml": _bot(support="support.yaml"), "support.yaml": saved}
    )
    check = await _lints_of_files(session, {"support.yaml": drafted})
    assert check.status == "warn"
    errors = [i["code"] for i in check.items if i["severity"] == "error"]
    assert errors == ["unknown-root"]
    assert not any(i.get("new") for i in check.items)


_DEEP = "(" * 400 + "args.amount == 1" + ")" * 400


@pytest.mark.usefixtures("stored")
async def test_when_a_constraint_is_nested_too_deeply_then_composes_fails(
    session,
) -> None:
    deep = f"tools:\n  refund: {{ mode: allow, constraint: '{_DEEP}' }}\n"
    result = await _run(session, {"grants.yaml": deep})
    assert _statuses(result) == ["pass", "fail"] + ["skipped"] * 3
    assert _by_id(result, "composes").detail == "a constraint is nested too deeply"


@pytest.mark.usefixtures("stored")
async def test_when_a_boundary_constraint_is_nested_too_deeply_then_boundary_fails(
    session,
) -> None:
    # Not imported, so only the boundary comparison lowers it.
    deep = (
        f"boundary:\n  tools:\n    refund: {{ mode: allow, constraint: '{_DEEP}' }}\n"
    )
    result = await _run(session, {"loose.yaml": deep})
    assert _statuses(result) == ["pass", "pass", "pass", "pass", "fail"]
    assert _by_id(result, "boundary").detail == "a constraint is nested too deeply"


async def test_when_saved_errors_pass_the_cap_then_the_new_error_is_still_listed(
    session,
) -> None:
    # MAX_ITEMS + 1 saved error lints, each on its own path, then one more.
    def grants(count: int) -> str:
        rules = "".join(
            f"  t{i}: {{ mode: allow, constraint: 'not (p{i}.x == 1)' }}\n"
            for i in range(count)
        )
        return f"tools:\n{rules}"

    await _store(
        session,
        {"policy.yaml": _bot(support="grants.yaml"), "grants.yaml": grants(51)},
    )
    check = await _lints_of_files(session, {"grants.yaml": grants(52)})
    assert check.status == "fail"
    assert check.detail.startswith("1 new error lint(s)")
    assert check.items[0]["new"] is True
    assert "p51" in check.items[0]["message"]
