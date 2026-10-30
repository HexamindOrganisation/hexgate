import json

import pytest
import respx

from hexgate_mcp.tools.policy import NOT_IN_FORCE
from tests.conftest import API_URL, JWKS_URL, OTHER_PROJECT, PROJECT, call_tool, jwks

BASE = f"/v1/projects/{PROJECT}"
STAMP = {"content_hash": "h", "updated_at": "2026-10-22T00:00:00Z"}
ENTRY = {"name": "policy.yaml", "content": "version: 1\n", **STAMP}
CAP = {"name": "caps/refunds.yaml", "content": "tools: {}\n", **STAMP}
MODULE = {"tier": "capability", "path": "refunds", "content": "x: 1\n", **STAMP}
DRAFT = {"name": "caps/refunds.yaml", "content": "tools: {a: allow}\n"}
ENTRY_DRAFT = {"name": "policy.yaml", "content": "version: 1\n"}
NEW_FILE_DRAFT = {"name": "caps/billing.yaml", "content": "tools: {}\n"}
DRY_RUN = {"role": "support", "tool": "refund"}
FULL_DRY_RUN = {
    **DRY_RUN,
    "agent": "billing",
    "args": {"amount": 500},
    "attributes": {"tier": "gold"},
    "draft": DRAFT,
}
# What the API answers on each route these tools call.
ANSWERS = {
    ("GET", "/policy-files"): [ENTRY, CAP],
    ("GET", "/policy-modules"): [MODULE],
    ("GET", "/policy/resolve"): {"roles": {}},
    ("GET", "/policy/graph"): {
        "nodes": [
            {"id": "agent:billing", "kind": "agent", "label": "billing"},
            {"id": "tool:refund", "kind": "tool", "label": "refund"},
        ],
        "edges": [],
        "roles": ["support"],
    },
    ("GET", "/policy-roles"): {"roles": {"support": {"*": ["refunds"]}}},
    ("GET", "/policy/check"): {"ok": True, "lints": []},
    ("POST", "/policy/preview"): {"resolved": {}, "lints": []},
    ("POST", "/policy/test"): {"outcome": "allow"},
}
# The tools that read the resolved policy, so refuse a project with no store.
RESOLVED = [
    ("policy_resolve", {}, "/policy/resolve"),
    ("policy_graph", {}, "/policy/graph"),
    ("policy_check", {}, "/policy/check"),
    ("policy_dry_run", DRY_RUN, "/policy/test"),
]
# One call of every tool here, each with the arguments it needs.
CALLS = [
    ("policy_list_files", {}),
    ("policy_read_file", {"name": "policy.yaml"}),
    ("policy_resolve", {}),
    ("policy_graph", {}),
    ("policy_list_roles", {}),
    ("policy_check", {}),
    ("policy_dry_run", DRY_RUN),
]


@pytest.fixture
def token(key, respx_mock) -> str:
    """Mocks inside respx_mock, so the JWKS route is undone after the test."""
    respx.get(JWKS_URL).respond(json=jwks(key))
    return key.mint()


def mock_api(**overrides) -> None:
    """Every route in ANSWERS, with `overrides` by path."""
    for (method, path), answer in ANSWERS.items():
        url = f"{API_URL}{BASE}{path}"
        respx.route(method=method, url=url).respond(json=overrides.get(path, answer))
    # Last, so it only answers what the rest don't: a call that should never
    # have been sent is then recorded, not raised inside the tool.
    respx.route(host="api.internal").respond(json=[])


def api_requests() -> list:
    """What the tool sent the API, the JWKS fetch left out."""
    return [c.request for c in respx.calls if c.request.url.path.startswith("/v1/")]


def sent(request) -> tuple:
    """A request as (method, path, query or JSON body)."""
    path = request.url.path.removeprefix(BASE)
    if request.method == "GET":
        return ("GET", path, dict(request.url.params))
    return (request.method, path, json.loads(request.content))


def call(token: str, name: str, /, **arguments) -> dict:
    return call_tool(token, name, {"project_id": PROJECT, **arguments})


def error(result: dict) -> str:
    assert result["isError"]
    return result["content"][0]["text"]


@pytest.mark.parametrize(
    ("name", "arguments", "requests", "expected"),
    [
        pytest.param(
            "policy_list_files",
            {},
            [("GET", "/policy-files", {}), ("GET", "/policy-modules", {})],
            {
                "files": [
                    {"name": "policy.yaml", **STAMP},
                    {"name": "caps/refunds.yaml", **STAMP},
                ],
                # Beside policy.yaml the module store is kept but not read.
                "modules": {
                    "in_force": False,
                    "note": NOT_IN_FORCE,
                    "modules": [{"tier": "capability", "path": "refunds", **STAMP}],
                },
            },
            id="list_files-drops-content",
        ),
        pytest.param(
            "policy_read_file",
            {"name": "caps/refunds.yaml"},
            [("GET", "/policy-files", {})],
            CAP,
            id="read_file-file",
        ),
        pytest.param(
            "policy_read_file",
            {"name": "refunds", "tier": "capability"},
            [("GET", "/policy-modules", {})],
            MODULE,
            id="read_file-module-by-tier",
        ),
        pytest.param(
            "policy_resolve",
            {},
            # Unset: left out, so the API applies its own defaults.
            [("GET", "/policy-files", {}), ("GET", "/policy/resolve", {})],
            {"roles": {}},
            id="resolve-defaults",
        ),
        pytest.param(
            "policy_resolve",
            {"role": "support", "agent": "billing"},
            [
                ("GET", "/policy-files", {}),
                ("GET", "/policy/graph", {}),
                ("GET", "/policy/resolve", {"role": "support", "agent": "billing"}),
            ],
            {"roles": {}},
            id="resolve-role-and-agent",
        ),
        pytest.param(
            "policy_resolve",
            {"agent": "*"},
            # The generic view is every project's: no agent lookup.
            [("GET", "/policy-files", {}), ("GET", "/policy/resolve", {"agent": "*"})],
            {"roles": {}},
            id="resolve-generic-agent",
        ),
        pytest.param(
            "policy_graph",
            {"role": "support"},
            [
                ("GET", "/policy-files", {}),
                ("GET", "/policy/graph", {"role": "support"}),
            ],
            ANSWERS["GET", "/policy/graph"],
            id="graph-role",
        ),
        pytest.param(
            "policy_graph",
            {},
            [("GET", "/policy-files", {}), ("GET", "/policy/graph", {})],
            ANSWERS["GET", "/policy/graph"],
            id="graph-every-role",
        ),
        pytest.param(
            "policy_list_roles",
            {},
            [("GET", "/policy-roles", {}), ("GET", "/policy-files", {})],
            {
                "in_force": False,
                "note": NOT_IN_FORCE,
                **ANSWERS["GET", "/policy-roles"],
            },
            id="list_roles",
        ),
        pytest.param(
            "policy_check",
            {},
            [("GET", "/policy-files", {}), ("GET", "/policy/check", {})],
            {"ok": True, "lints": []},
            id="check-saved",
        ),
        pytest.param(
            "policy_check",
            {"draft": DRAFT},
            [("GET", "/policy-files", {}), ("POST", "/policy/preview", DRAFT)],
            {"resolved": {}, "lints": []},
            id="check-draft",
        ),
        pytest.param(
            "policy_check",
            {"draft": ENTRY_DRAFT},
            # The entry file starts a compose store: no lookup needed.
            [("POST", "/policy/preview", ENTRY_DRAFT)],
            {"resolved": {}, "lints": []},
            id="check-entry-draft",
        ),
        pytest.param(
            "policy_dry_run",
            DRY_RUN,
            # Unset fields left out, so the API applies its own defaults.
            [("GET", "/policy-files", {}), ("POST", "/policy/test", DRY_RUN)],
            {"outcome": "allow"},
            id="dry_run-defaults",
        ),
        pytest.param(
            "policy_dry_run",
            FULL_DRY_RUN,
            [
                ("GET", "/policy-files", {}),
                ("GET", "/policy/graph", {}),
                ("POST", "/policy/test", FULL_DRY_RUN),
            ],
            {"outcome": "allow"},
            id="dry_run-every-argument",
        ),
        pytest.param(
            "policy_dry_run",
            # A draft policy.yaml may declare a new agent: no agent lookup.
            {**DRY_RUN, "agent": "triage", "draft": ENTRY_DRAFT},
            [
                (
                    "POST",
                    "/policy/test",
                    {**DRY_RUN, "agent": "triage", "draft": ENTRY_DRAFT},
                )
            ],
            {"outcome": "allow"},
            id="dry_run-entry-draft",
        ),
    ],
)
def test_policy_tools_happy_path(token, name, arguments, requests, expected):
    mock_api()

    result = call(token, name, **arguments)

    assert not result["isError"], result["content"][0]["text"]
    assert "structuredContent" not in result
    assert json.loads(result["content"][0]["text"]) == expected
    assert [sent(r) for r in api_requests()] == requests
    assert {r.headers["authorization"] for r in api_requests()} == {f"Bearer {token}"}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"name": "refunds"}, "no policy file 'refunds'; a policy module also needs"),
        ({"name": "refunds", "tier": "boundary"}, "no boundary module 'refunds'; a"),
        ({"name": "nope", "tier": "capability"}, "no capability module 'nope'; a"),
        ({"name": "policy.yaml", "tier": "capability"}, "a policy file takes no"),
    ],
)
def test_when_no_file_matches_then_policy_read_file_refuses(token, arguments, message):
    mock_api()

    assert message in error(call(token, "policy_read_file", **arguments))


def test_when_project_has_no_entry_file_then_modules_and_roles_are_in_force(token):
    mock_api(**{"/policy-files": [CAP]})

    files = json.loads(call(token, "policy_list_files")["content"][0]["text"])
    roles = json.loads(call(token, "policy_list_roles")["content"][0]["text"])

    assert files["modules"] == [{"tier": "capability", "path": "refunds", **STAMP}]
    assert roles == ANSWERS["GET", "/policy-roles"]


def test_when_name_and_tier_are_swapped_then_policy_read_file_refuses(token):
    mock_api()

    result = call(token, "policy_read_file", name="capability", tier="refunds")

    assert "tier" in error(result)
    assert api_requests() == []


@pytest.mark.parametrize("agent", ["biling", "refund"])
def test_when_agent_is_not_declared_then_policy_resolve_refuses(token, agent):
    # A tool node's label is not an agent.
    mock_api()

    message = error(call(token, "policy_resolve", agent=agent))

    assert (
        f"agent {agent!r} is not declared in the policy (declared: ['billing'])"
        in message
    )
    assert 'runs under the generic "*" view' in message
    assert all(sent(r)[1] != "/policy/resolve" for r in api_requests())


def test_when_role_is_unknown_then_policy_graph_refuses(token):
    mock_api()

    message = error(call(token, "policy_graph", role="Support"))

    assert message.endswith("role 'Support' not defined (known: ['support'])")


@pytest.mark.parametrize(("name", "arguments", "_"), RESOLVED)
def test_when_project_has_no_policy_store_then_resolved_views_refuse(
    token, name, arguments, _
):
    mock_api(**{"/policy-files": [CAP], "/policy-roles": {"roles": {}}})

    assert "each agent enforces its own policy" in error(call(token, name, **arguments))
    assert [r.url.path for r in api_requests()][-1] == f"{BASE}/policy-roles"


@pytest.mark.parametrize(("name", "arguments", "path"), RESOLVED)
def test_when_project_has_role_bindings_only_then_resolved_views_answer(
    token, name, arguments, path
):
    mock_api(**{"/policy-files": []})

    assert not call(token, name, **arguments)["isError"]
    assert sent(api_requests()[-1])[1] == path


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("policy_check", {"draft": DRAFT}),
        ("policy_dry_run", {**DRY_RUN, "draft": DRAFT}),
    ],
)
@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        # Module store: the preview could only answer link-error, and the test
        # route would evaluate the saved policy, ignoring the draft.
        ({"support": {"*": ["refunds"]}}, "a draft file cannot apply"),
        # Classic: a retry without the draft would be refused too, so the
        # refusal names the real reason at once.
        ({}, "each agent enforces its own policy"),
    ],
)
def test_when_project_has_no_entry_file_then_a_draft_is_refused(
    token, name, arguments, roles, expected
):
    mock_api(**{"/policy-files": [CAP], "/policy-roles": {"roles": roles}})

    message = error(call(token, name, **arguments))

    assert expected in message
    assert {r.method for r in api_requests()} == {"GET"}


def test_when_api_refuses_then_the_tool_error_carries_its_detail(token):
    detail = "role 'nope' not defined (known: ['support'])"
    mock_api()
    # Registered after: respx replaces the route with the same pattern.
    respx.get(f"{API_URL}{BASE}/policy/resolve").respond(404, json={"detail": detail})

    result = call(token, "policy_resolve", role="nope")

    assert error(result).endswith(f": 404 not found: {detail}")


@pytest.mark.parametrize(("name", "arguments"), CALLS)
@respx.mock
def test_when_token_lacks_policy_read_then_refused_before_any_request(
    key, name, arguments
):
    respx.get(JWKS_URL).respond(json=jwks(key))
    mock_api()

    result = call(key.mint(scope="projects:read"), name, **arguments)

    assert "'policy:read'" in error(result)
    assert api_requests() == []


@pytest.mark.parametrize(("name", "arguments"), CALLS)
def test_when_project_is_not_granted_then_refused_before_any_request(
    token, name, arguments
):
    mock_api()

    result = call_tool(token, name, {"project_id": OTHER_PROJECT, **arguments})

    assert OTHER_PROJECT in error(result)
    assert api_requests() == []


@pytest.mark.parametrize(
    ("name", "arguments"),
    [("policy_list_files", {}), ("policy_check", {"draft": ENTRY_DRAFT})],
)
@respx.mock
def test_when_project_id_holds_a_slash_then_it_is_sent_percent_encoded(
    key, name, arguments
):
    respx.get(JWKS_URL).respond(json=jwks(key))
    api = respx.route(host="api.internal").respond(json=[])

    call_tool(key.mint(projects=["a/b"]), name, {"project_id": "a/b", **arguments})

    assert api.calls.last.request.url.raw_path.startswith(b"/v1/projects/a%2Fb/")


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("policy_check", {"draft": NEW_FILE_DRAFT}),
        ("policy_dry_run", {**DRY_RUN, "draft": NEW_FILE_DRAFT}),
    ],
)
def test_when_draft_is_a_file_not_stored_then_it_is_refused(token, name, arguments):
    # Nothing imports a new file yet, so the API would answer for the saved policy.
    mock_api()

    message = error(call(token, name, **arguments))

    assert "save it first with policy_save" in message
    assert {r.method for r in api_requests()} == {"GET"}


def test_when_agent_is_not_declared_then_policy_dry_run_refuses(token):
    mock_api()

    message = error(call(token, "policy_dry_run", **DRY_RUN, agent="biling"))

    assert "agent 'biling' is not declared in the policy" in message
    assert {r.method for r in api_requests()} == {"GET"}
