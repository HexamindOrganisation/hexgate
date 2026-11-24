"""apply_change: a checked policy change, its record and the rebuilt bundles,
saved in one commit or not at all."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError
from sqlmodel import select

from hexgate_api.constants import DEFAULT_ORG_ID, DEFAULT_PROJECT_ID, DEFAULT_USER_ID
from hexgate_api.features.agents import service as agents_service
from hexgate_api.features.policy_modules import service
from hexgate_api.features.policy_modules.apply import apply_change
from hexgate_api.models import Agent, AssistantChange, PolicyFile, Project
from hexgate_api.schemas import (
    APPLY_MAX_FILE_BYTES,
    AgentManifest,
    PolicyApplyRequest,
)

PID = DEFAULT_PROJECT_ID
ACTOR = DEFAULT_USER_ID
CLIENT = "hexgate-dashboard-assistant"
CHECKS = ["yaml", "composes", "lints", "dry_runs", "boundary", "bundle"]

_ENTRY = """\
boundary:
  tools:
    refund: { mode: allow, constraint: 'args.amount <= 1000' }
    view: { mode: allow }
agents:
  bot:
    roles:
      support: { import: [ grants.yaml ] }
"""
_GRANTS = "tools:\n  refund: { mode: allow, constraint: 'args.amount <= 500' }\n"
# The change most tests apply: support may also view, and refunds less.
_NEW_GRANTS = (
    "tools:\n"
    "  refund: { mode: allow, constraint: 'args.amount <= 200' }\n"
    "  view: { mode: allow }\n"
)
_VIEW = {"role": "support", "tool": "view", "agent": "bot", "expect": "allow"}
_OLD_WASM = b"old-wasm"
_AMOUNT = {"amount": {"title": "Amount", "type": "number"}}


@pytest.fixture
def compiled(monkeypatch) -> list[str]:
    """Every policy the bundle build is given; each build succeeds."""
    seen: list[str] = []

    def compile_bundle(policy_yaml, sign):
        seen.append(policy_yaml)
        return (b"new-wasm", "manifest", b"sig")

    monkeypatch.setattr(agents_service, "compile_bundle", compile_bundle)
    return seen


@pytest.fixture
async def project(session_factory, compiled):
    """The saved files and ``bot``, registered, enforcing ``_OLD_WASM``."""
    async with session_factory() as session:
        for name, content in {"policy.yaml": _ENTRY, "grants.yaml": _GRANTS}.items():
            await service.upsert_file(
                session, project_id=PID, name=name, content=content, actor_user_id=ACTOR
            )
        manifest = AgentManifest.model_validate(
            {
                "name": "bot",
                "framework": "hexgate",
                "tools": [
                    {
                        "name": t,
                        "description": t,
                        "input_schema": {"properties": props, "required": []},
                    }
                    for t, props in (("refund", _AMOUNT), ("view", {}))
                ],
            }
        )
        await agents_service.register_manifest(
            session, PID, manifest, sign=lambda _: b"sig"
        )
        agent = await agents_service.get_agent(session, PID, "bot")
        agent.compiled_wasm = _OLD_WASM
        session.add(agent)
        await session.commit()
    compiled.clear()


def _request(
    *files: tuple[str, str, str | None], samples=(_VIEW,)
) -> PolicyApplyRequest:
    return PolicyApplyRequest(
        files=[{"name": n, "content": c, "base_hash": h} for n, c, h in files],
        summary="support may view; refunds capped at 200",
        samples=list(samples),
        session_id="ses_1",
    )


def _h(text: str) -> str:
    return service.text_hash(text)


async def _apply(session_factory, request: PolicyApplyRequest):
    """Apply, then commit the same session, as a caller that writes something
    of its own afterwards would: a refused change must leave nothing in it."""
    async with session_factory() as session:
        out = await apply_change(session, PID, request, ACTOR, CLIENT)
        await session.commit()
        return out


async def _stored(session_factory) -> tuple[dict[str, PolicyFile], list, bytes | None]:
    """What a fresh session reads: the files, the change rows, bot's bundle."""
    async with session_factory() as session:
        files = {r.name: r for r in await service.list_files(session, PID)}
        changes = (await session.exec(select(AssistantChange))).all()
        bot = select(Agent).where(Agent.name == "bot")
        agent = (await session.exec(bot)).one_or_none()
        return files, list(changes), agent and agent.compiled_wasm


async def _assert_nothing_written(session_factory) -> None:
    files, changes, wasm = await _stored(session_factory)
    assert {n: f.content for n, f in files.items()} == {
        "policy.yaml": _ENTRY,
        "grants.yaml": _GRANTS,
    }
    assert all(f.updated_via_change_id is None for f in files.values())
    assert changes == []
    assert wasm == _OLD_WASM


@pytest.mark.usefixtures("project")
async def test_apply_change_happy_path(session_factory, compiled) -> None:
    request = _request(
        ("grants.yaml", _NEW_GRANTS, _h(_GRANTS)), ("extra.yaml", "tools: {}\n", None)
    )

    out = await _apply(session_factory, request)

    assert out.applied, out.checks
    assert [c["id"] for c in out.checks] == CHECKS
    assert [c["status"] for c in out.checks] == ["pass"] * 6
    assert [(f.name, f.before_hash, f.after_hash) for f in out.files] == [
        ("grants.yaml", _h(_GRANTS), _h(_NEW_GRANTS)),
        ("extra.yaml", None, _h("tools: {}\n")),
    ]
    files, [change], wasm = await _stored(session_factory)
    assert out.change_id == change.id and change.id.startswith("chg_")
    assert files["grants.yaml"].content == _NEW_GRANTS
    assert files["grants.yaml"].updated_by_user_id == ACTOR
    assert files["extra.yaml"].created_by_user_id == ACTOR
    # Every file the change wrote names it; the untouched entry does not.
    assert files["grants.yaml"].updated_via_change_id == change.id
    assert files["extra.yaml"].updated_via_change_id == change.id
    assert files["policy.yaml"].updated_via_change_id is None
    assert (change.session_id, change.client_id) == ("ses_1", CLIENT)
    assert change.created_by_user_id == ACTOR
    assert change.files[0] == {
        "name": "grants.yaml",
        "before_content": _GRANTS,
        "before_hash": _h(_GRANTS),
        "after_content": _NEW_GRANTS,
        "after_hash": _h(_NEW_GRANTS),
    }
    assert change.files[1]["before_content"] is None
    assert change.samples == [{**_VIEW, "args": {}, "attributes": {}}]
    assert change.checks == out.checks
    # The effect is the diff taken before the save: view arrives for support.
    [view] = [c for c in change.effect["changes"] if c["tool"] == "view"]
    assert (view["role"], view["before"]) == ("support", None)
    # The bundle was built from the change, and is the one now enforced.
    assert any("200" in policy for policy in compiled)
    assert wasm == b"new-wasm"


@pytest.mark.parametrize(
    ("files", "stale"),
    [
        # One file of two changed since it was read.
        (
            [("policy.yaml", _ENTRY, _h(_ENTRY)), ("grants.yaml", _NEW_GRANTS, "x")],
            ["grants.yaml"],
        ),
        # Null: the file must not exist yet, and it does.
        ([("grants.yaml", _NEW_GRANTS, None)], ["grants.yaml"]),
        # A hash: the file must exist, and it does not.
        ([("new.yaml", "tools: {}\n", _h("tools: {}\n"))], ["new.yaml"]),
    ],
)
@pytest.mark.usefixtures("project")
async def test_when_a_base_hash_does_not_match_then_stale_and_nothing_written(
    session_factory, files, stale
) -> None:
    with pytest.raises(service.StaleBaseError) as exc:
        await _apply(session_factory, _request(*files))

    assert exc.value.files == stale
    await _assert_nothing_written(session_factory)


@pytest.mark.usefixtures("project")
async def test_when_another_admin_edited_only_a_comment_then_stale(
    session_factory,
) -> None:
    """The edit leaves the parsed YAML, and so ``content_hash``, unchanged."""
    read = _h(_GRANTS)
    async with session_factory() as session:
        await service.upsert_file(
            session,
            project_id=PID,
            name="grants.yaml",
            content="# reviewed\n" + _GRANTS,
            actor_user_id=ACTOR,
        )

    with pytest.raises(service.StaleBaseError):
        await _apply(session_factory, _request(("grants.yaml", _NEW_GRANTS, read)))

    files, changes, _ = await _stored(session_factory)
    assert files["grants.yaml"].content == "# reviewed\n" + _GRANTS
    assert changes == []


@pytest.mark.usefixtures("project")
async def test_when_one_file_of_three_is_bad_then_nothing_is_written(
    session_factory,
) -> None:
    request = _request(
        ("grants.yaml", _NEW_GRANTS, _h(_GRANTS)),
        ("a.yaml", "tools: {}\n", None),
        ("b.yaml", "tools: [", None),
    )

    out = await _apply(session_factory, request)

    assert not out.applied and out.change_id is None
    assert [c["status"] for c in out.checks] == ["fail"] + ["skipped"] * 5
    assert out.checks[-1] == {
        "id": "bundle",
        "status": "skipped",
        "detail": "yaml failed",
        "items": [],
    }
    assert out.files == []
    await _assert_nothing_written(session_factory)


@pytest.mark.usefixtures("project")
async def test_when_a_boundary_is_widened_then_nothing_is_written(
    session_factory,
) -> None:
    entry = _ENTRY.replace("<= 1000", "<= 5000")

    request = _request(("policy.yaml", entry, _h(_ENTRY)), samples=())

    out = await _apply(session_factory, request)

    assert not out.applied
    assert [c["status"] for c in out.checks][-2:] == ["fail", "skipped"]
    assert [b["kind"] for b in out.boundary_changes] == ["widened"]
    await _assert_nothing_written(session_factory)


@pytest.mark.usefixtures("project")
async def test_when_recompile_project_returns_none_then_nothing_is_written(
    session_factory, monkeypatch
) -> None:
    async def unbuildable(*_, **__):
        return None

    monkeypatch.setattr(agents_service, "recompile_project", unbuildable)

    out = await _apply(
        session_factory, _request(("grants.yaml", _NEW_GRANTS, _h(_GRANTS)))
    )

    assert not out.applied and out.change_id is None
    assert [c["status"] for c in out.checks] == ["pass"] * 5 + ["fail"]
    assert out.files == []
    await _assert_nothing_written(session_factory)


def _opa_crashes(*_):
    raise RuntimeError("opa crashed")


@pytest.mark.parametrize("compile_bundle", [lambda *_: None, _opa_crashes])
@pytest.mark.usefixtures("project")
async def test_when_an_agent_bundle_cannot_be_built_then_nothing_is_written(
    session_factory, monkeypatch, compile_bundle
) -> None:
    """The real rebuild, with opa failing or crashing: the old bundle stays
    enforced."""
    monkeypatch.setattr(agents_service, "compile_bundle", compile_bundle)

    out = await _apply(
        session_factory, _request(("grants.yaml", _NEW_GRANTS, _h(_GRANTS)))
    )

    assert not out.applied
    assert out.checks[-1]["status"] == "fail"
    await _assert_nothing_written(session_factory)


@pytest.mark.usefixtures("project")
async def test_when_the_commit_fails_then_it_raises_and_nothing_is_written(
    session_factory,
) -> None:
    from sqlmodel.ext.asyncio.session import AsyncSession

    async def failing_commit(self):
        raise RuntimeError("connection lost")

    request = _request(("grants.yaml", _NEW_GRANTS, _h(_GRANTS)))
    async with session_factory() as session:
        # Scoped, so the compile_bundle stub of ``compiled`` stays in place.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(AsyncSession, "commit", failing_commit)
            with pytest.raises(RuntimeError):
                await apply_change(session, PID, request, ACTOR, CLIENT)
        await session.commit()  # the caller's next write

    await _assert_nothing_written(session_factory)


@pytest.mark.usefixtures("project")
async def test_when_two_applies_read_the_same_base_then_one_is_stale(
    session_factory,
) -> None:
    """Both read ``_GRANTS``; the project lock makes the second see the first's
    commit, so it is stale instead of overwriting it."""
    first = _NEW_GRANTS
    second = _NEW_GRANTS.replace("<= 200", "<= 300")

    outcomes = await asyncio.gather(
        _apply(session_factory, _request(("grants.yaml", first, _h(_GRANTS)))),
        _apply(session_factory, _request(("grants.yaml", second, _h(_GRANTS)))),
        return_exceptions=True,
    )

    stale = [o for o in outcomes if isinstance(o, service.StaleBaseError)]
    applied = [o for o in outcomes if not isinstance(o, BaseException)]
    assert len(stale) == 1 and len(applied) == 1, outcomes
    assert applied[0].applied
    files, changes, _ = await _stored(session_factory)
    [won] = [f.after_hash for f in applied[0].files]
    assert _h(files["grants.yaml"].content) == won
    assert len(changes) == 1


async def test_when_the_project_has_no_agents_then_the_change_is_applied(
    session_factory,
) -> None:
    """Nothing to build is not a failed build: the files are saved."""
    pid = "prj_no_agents"
    async with session_factory() as session:
        session.add(Project(id=pid, org_id=DEFAULT_ORG_ID, name="no agents"))
        for name, content in {"policy.yaml": _ENTRY, "grants.yaml": _GRANTS}.items():
            await service.upsert_file(
                session, project_id=pid, name=name, content=content, actor_user_id=ACTOR
            )
    request = _request(("grants.yaml", _NEW_GRANTS, _h(_GRANTS)), samples=())

    async with session_factory() as session:
        out = await apply_change(session, pid, request, ACTOR, CLIENT)

    assert out.applied
    assert out.checks[-1]["detail"] == "0 agent bundle(s) built"
    async with session_factory() as session:
        stored = await service.get_file(session, pid, "grants.yaml")
        assert stored.updated_via_change_id == out.change_id


async def test_when_the_project_has_no_policy_yaml_then_the_layout_error(
    session_factory,
) -> None:
    async with session_factory() as session:
        await service.upsert_file(
            session,
            project_id=PID,
            name="grants.yaml",
            content=_GRANTS,
            actor_user_id=ACTOR,
        )

    request = _request(("policy.yaml", _ENTRY, None))
    with pytest.raises(service.NoEntryFileError):
        await _apply(session_factory, request)

    files, changes, _ = await _stored(session_factory)
    assert set(files) == {"grants.yaml"} and changes == []


async def test_recompile_project_when_commit_is_false_then_a_rollback_drops_the_bundles(
    session_factory, compiled, project
) -> None:
    async with session_factory() as session:
        built = await agents_service.recompile_project(
            session, PID, lambda _: b"sig", commit=False
        )
        assert built
        await session.rollback()

    _, _, wasm = await _stored(session_factory)
    assert wasm == _OLD_WASM


# --- the request's caps -------------------------------------------------------


def _file(name: str = "a.yaml", content: str = "tools: {}\n", **extra) -> dict:
    return {"name": name, "content": content, "base_hash": None, **extra}


def _body(**fields) -> dict:
    return {"files": [_file()], "summary": "s", **fields}


@pytest.mark.parametrize(
    "body",
    [
        _body(files=[]),
        _body(files=[_file(f"f{i}.yaml") for i in range(11)]),
        _body(files=[_file(), _file()]),
        _body(files=[_file(content="x" * (APPLY_MAX_FILE_BYTES + 1))]),
        # 2-byte characters: under the cap in characters, over it in bytes.
        _body(files=[_file(content="é" * (APPLY_MAX_FILE_BYTES // 2 + 1))]),
        _body(files=[{"name": "a.yaml", "content": "x"}]),  # no base_hash
        _body(files=[_file(edits=[])]),
        _body(summary="s" * 2001),
        _body(samples=[_VIEW] * 21),
        _body(samples=[{**_VIEW, "expect": "maybe"}]),
        _body(session_id="s" * 65),
        _body(drafts=[]),
    ],
)
def test_when_a_request_breaks_a_cap_then_it_is_refused(body) -> None:
    with pytest.raises(ValidationError):
        PolicyApplyRequest.model_validate(body)


def test_policy_apply_request_happy_path() -> None:
    body = _body(
        files=[
            _file(f"f{i}.yaml", "é" * (APPLY_MAX_FILE_BYTES // 2)) for i in range(10)
        ],
        summary="s" * 2000,
        samples=[{"role": "r", "tool": "t", "expect": "deny"}] * 20,
        session_id="s" * 64,
    )

    request = PolicyApplyRequest.model_validate(body)

    assert request.samples[0].agent == "*"
    assert request.samples[0].attributes == {}
