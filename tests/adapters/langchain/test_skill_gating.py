"""deepagents skill activation gating on the in-place LangChain installer.

A ``read_file`` of a known ``SKILL.md`` decides under ``skill:<name>`` (a file
below it under ``skill.resource:<name>``) with the skill name, level, path and
content hash as decision args, once the policy declares skills. Hand-rolled
middleware and backend fakes: deepagents is not a test dependency.
"""

from __future__ import annotations

import hashlib
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.tools import BaseTool, StructuredTool

from hexgate.adapters.langchain import wrapper as wrapper_mod
from hexgate.adapters.langchain.skills import SkillKeyResolver, SkillPathIndex
from hexgate.adapters.langchain.tools import install_enforcer_on_tool
from hexgate.adapters.langchain.wrapper import wrap_langchain_agent
from hexgate.manifest.langchain import SkillLocation, read_skill_hash
from hexgate.security import AgentPolicy, PolicySet, ResolvedPolicy
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.security.policy_set import DEFAULT_ROLE_NAME

SKILL = "refunder"
ROOT = "/skills/project"
SKILL_DIR = f"{ROOT}/{SKILL}"
SKILL_MD = f"{SKILL_DIR}/SKILL.md"
RESOURCE = f"{SKILL_DIR}/references/limits.md"
BODY = "Refund only after checking the order."
SKILL_MD_BYTES = f"---\nname: {SKILL}\ndescription: refunds\n---\n{BODY}\n".encode()
DEEPAGENTS_SKILLS_MODULE = "deepagents.middleware.skills"
PIN = ["args.content_hash == consts.approved"]


def _digest(body: str) -> str:
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


class _Download:
    def __init__(self, path: str, content: bytes | None, error: str | None = None):
        self.path = path
        self.content = content
        self.error = error


class _Backend:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.listing = {
            ROOT: [{"path": SKILL_MD, "name": SKILL, "description": "refunds"}]
        }
        self.bodies = bodies

    def download_files(self, paths: list[str]) -> list[_Download]:
        return [
            _Download(p, self.bodies[p])
            if p in self.bodies
            else _Download(p, None, "file_not_found")
            for p in paths
        ]


class _SkillsMiddleware:
    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self.sources = [ROOT]
        self.source_labels = ["Project"]

    def before_agent(self, state: Any, runtime: Any) -> None:
        return None


class _DeepGraph:
    """A compiled graph binding ``read_file`` with its SkillsMiddleware hook node."""

    name = "deep"

    def __init__(self, bound: list[BaseTool], middleware: Any | None) -> None:
        self.nodes: dict[str, Any] = {
            "tools": SimpleNamespace(
                bound=SimpleNamespace(tools_by_name={t.name: t for t in bound})
            )
        }
        if middleware is not None:
            self.nodes["SkillsMiddleware.before_agent"] = SimpleNamespace(
                bound=SimpleNamespace(func=middleware.before_agent)
            )


def _read_file_tool(reads: list[str]) -> StructuredTool:
    def read_file(file_path: str) -> str:
        reads.append(file_path)
        return f"read:{file_path}"

    async def aread_file(file_path: str) -> str:
        reads.append(file_path)
        return f"read:{file_path}"

    return StructuredTool.from_function(
        func=read_file, coroutine=aread_file, name="read_file", description="Read."
    )


@pytest.fixture(autouse=True)
def fake_deepagents(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType(DEEPAGENTS_SKILLS_MODULE)
    module._list_skills = lambda backend, source: backend.listing.get(source, [])
    monkeypatch.setitem(sys.modules, DEEPAGENTS_SKILLS_MODULE, module)
    return module


@pytest.fixture
def decisions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    real_decide = PolicyEnforcer.decide

    def decide(self: PolicyEnforcer, tool_name: str, arguments: Any) -> Any:
        seen.append((tool_name, dict(arguments)))
        return real_decide(self, tool_name, arguments)

    monkeypatch.setattr(PolicyEnforcer, "decide", decide)
    return seen


def _engine(spec: dict[str, Any]) -> PolicySet:
    return PolicySet({DEFAULT_ROLE_NAME: AgentPolicy.model_validate(spec)})


def _skills_policy(**skill: Any) -> dict[str, Any]:
    return {
        "default_policy": {"mode": "allow"},
        "skills": {SKILL: {"mode": "allow", **skill}},
    }


def _pinned_policy() -> dict[str, Any]:
    return {**_skills_policy(constraints=PIN), "consts": {"approved": _digest(BODY)}}


class _Wrapped:
    def __init__(self, reads: list[str], tool: StructuredTool, backend: _Backend):
        self.reads = reads
        self.tool = tool
        self.backend = backend

    def read(self, path: str) -> Any:
        return self.tool.func(file_path=path)


def _wrap(
    monkeypatch: pytest.MonkeyPatch,
    spec: dict[str, Any],
    *,
    bodies: dict[str, bytes] | None = None,
    with_middleware: bool = True,
) -> _Wrapped:
    monkeypatch.setattr(
        wrapper_mod,
        "resolve_policy",
        lambda name, *, api_key, client=None: ResolvedPolicy(_engine(spec), None),
    )
    reads: list[str] = []
    tool = _read_file_tool(reads)
    backend = _Backend({SKILL_MD: SKILL_MD_BYTES} if bodies is None else bodies)
    middleware = _SkillsMiddleware(backend) if with_middleware else None
    wrap_langchain_agent(agent=_DeepGraph([tool], middleware), tools=[], api_key="k")
    return _Wrapped(reads, tool, backend)


def _enforcer(spec: dict[str, Any]) -> PolicyEnforcer:
    return PolicyEnforcer(_engine(spec), agent_name="deep")


def _resolver(spec: dict[str, Any]) -> SkillKeyResolver:
    backend = _Backend({SKILL_MD: SKILL_MD_BYTES})
    index = SkillPathIndex.from_locations([SkillLocation(SKILL, SKILL_MD, backend)])
    return SkillKeyResolver(_enforcer(spec), index, read_skill_hash)


# --- regression guard -------------------------------------------------------


def test_no_resolver_behaves_exactly_as_before(decisions) -> None:
    reads: list[str] = []
    tool = install_enforcer_on_tool(
        _read_file_tool(reads), enforcer=_enforcer(_skills_policy())
    )

    result = tool.func(file_path=SKILL_MD)

    assert result == f"read:{SKILL_MD}"
    assert decisions == [("read_file", {"file_path": SKILL_MD})]


def test_resolver_is_applied_to_graph_discovered_read_file(
    monkeypatch, decisions
) -> None:
    wrapped = _wrap(monkeypatch, _skills_policy())

    wrapped.read(SKILL_MD)

    assert [key for key, _ in decisions] == [f"skill:{SKILL}"]


# --- mapping ----------------------------------------------------------------


def test_reading_a_skill_md_decides_under_the_skill_key(monkeypatch, decisions) -> None:
    wrapped = _wrap(monkeypatch, _skills_policy())

    result = wrapped.read(SKILL_MD)

    assert result == f"read:{SKILL_MD}"
    [(key, args)] = decisions
    assert key == f"skill:{SKILL}"
    assert args["skill"] == SKILL
    assert args["via"] == "instructions"
    assert args["file_path"] == SKILL_MD


def test_reading_a_skill_resource_uses_the_resource_key(monkeypatch, decisions) -> None:
    wrapped = _wrap(monkeypatch, _skills_policy())

    wrapped.read(RESOURCE)

    [(key, args)] = decisions
    assert key == f"skill.resource:{SKILL}"
    assert args["via"] == "resource"


def test_a_respelled_skill_md_path_still_decides_under_the_skill_key(
    monkeypatch, decisions
) -> None:
    wrapped = _wrap(monkeypatch, _skills_policy())

    wrapped.read(f"{SKILL_DIR}/references/../SKILL.md")

    assert [key for key, _ in decisions] == [f"skill:{SKILL}"]


def test_reading_an_ordinary_file_decides_under_read_file(
    monkeypatch, decisions
) -> None:
    wrapped = _wrap(monkeypatch, _skills_policy())

    wrapped.read(f"{ROOT}/README.md")

    assert [key for key, _ in decisions] == ["read_file"]


def test_unknown_path_is_not_treated_as_a_skill(monkeypatch, decisions) -> None:
    wrapped = _wrap(monkeypatch, _skills_policy())

    wrapped.read(f"{ROOT}/{SKILL}-archive/SKILL.md")

    assert [key for key, _ in decisions] == ["read_file"]


def test_non_string_file_path_is_ignored() -> None:
    assert _resolver(_skills_policy())("read_file", {"file_path": 3}) is None


def test_other_tools_are_never_resolved() -> None:
    assert _resolver(_skills_policy())("write_file", {"file_path": SKILL_MD}) is None


def test_engagement_gate_off_decides_on_tool_name(monkeypatch, decisions) -> None:
    wrapped = _wrap(monkeypatch, {"default_policy": {"mode": "allow"}})

    wrapped.read(SKILL_MD)

    assert [key for key, _ in decisions] == ["read_file"]


def test_missing_middleware_gates_nothing_as_a_skill(monkeypatch, decisions) -> None:
    wrapped = _wrap(monkeypatch, _skills_policy(), with_middleware=False)

    wrapped.read(SKILL_MD)

    assert [key for key, _ in decisions] == ["read_file"]


# --- denial -----------------------------------------------------------------


def test_denied_skill_returns_the_structured_error(monkeypatch) -> None:
    wrapped = _wrap(monkeypatch, _skills_policy(mode="deny"))

    result = wrapped.read(SKILL_MD)

    assert result["ok"] is False
    assert result["error"]["type"] == "policy_denied"
    assert result["error"]["message"] == (
        f"skill {SKILL!r} is not permitted by this agent's policy. "
        "Do not attempt this task without it."
    )


def test_denied_skill_does_not_read_the_file(monkeypatch) -> None:
    wrapped = _wrap(monkeypatch, _skills_policy(mode="deny"))

    wrapped.read(SKILL_MD)

    assert wrapped.reads == []


def test_unlisted_skill_denies_under_a_permissive_default(monkeypatch) -> None:
    wrapped = _wrap(
        monkeypatch,
        {"default_policy": {"mode": "allow"}, "skills": {"other": {"mode": "allow"}}},
    )

    result = wrapped.read(SKILL_MD)

    assert result["ok"] is False
    assert wrapped.reads == []


def test_held_skill_names_the_held_level(monkeypatch) -> None:
    wrapped = _wrap(monkeypatch, _skills_policy(mode="approval_required"))

    result = wrapped.read(RESOURCE)

    assert result["ok"] is False
    assert (
        "requires human approval before its resource is read"
        in (result["error"]["message"])
    )


@pytest.mark.asyncio
async def test_async_read_is_gated_under_the_skill_key(monkeypatch, decisions) -> None:
    wrapped = _wrap(monkeypatch, _skills_policy(mode="deny"))

    result = await wrapped.tool.coroutine(file_path=SKILL_MD)

    assert result["ok"] is False
    assert wrapped.reads == []
    assert [key for key, _ in decisions] == [f"skill:{SKILL}"]


# --- content pinning --------------------------------------------------------


def test_content_hash_is_in_the_decision_args(monkeypatch, decisions) -> None:
    wrapped = _wrap(monkeypatch, _skills_policy())

    wrapped.read(RESOURCE)

    [(_, args)] = decisions
    assert args["content_hash"] == _digest(BODY)


def test_pinned_hash_allows_matching_content(monkeypatch) -> None:
    wrapped = _wrap(monkeypatch, _pinned_policy())

    assert wrapped.read(SKILL_MD) == f"read:{SKILL_MD}"


def test_pinned_hash_denies_changed_content(monkeypatch) -> None:
    wrapped = _wrap(monkeypatch, _pinned_policy())
    wrapped.backend.bodies[SKILL_MD] = b"---\nname: refunder\n---\nRefund everything."

    result = wrapped.read(SKILL_MD)

    assert result["ok"] is False
    assert wrapped.reads == []


def test_missing_hash_denies_a_pinned_skill(monkeypatch, decisions) -> None:
    wrapped = _wrap(monkeypatch, _pinned_policy(), bodies={})

    result = wrapped.read(SKILL_MD)

    assert result["ok"] is False
    assert decisions[0][1]["content_hash"] is None
