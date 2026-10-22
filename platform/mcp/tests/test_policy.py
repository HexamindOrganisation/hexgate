import json

import pytest
import respx

from tests.conftest import API_URL, JWKS_URL, OTHER_PROJECT, PROJECT, call_tool, jwks

BASE = f"/v1/projects/{PROJECT}"
STAMP = {"content_hash": "h", "updated_at": "2026-10-22T00:00:00Z"}
ENTRY = {"name": "policy.yaml", "content": "version: 1\n", **STAMP}
CAP = {"name": "caps/refunds.yaml", "content": "tools: {}\n", **STAMP}
MODULE = {"tier": "capability", "path": "refunds", "content": "x: 1\n", **STAMP}
# What the API answers on each route these tools call.
ANSWERS = {
    ("GET", "/policy-files"): [ENTRY, CAP],
    ("GET", "/policy-modules"): [MODULE],
    ("GET", "/policy/resolve"): {"roles": {}},
    ("GET", "/policy/graph"): {"nodes": [], "edges": [], "roles": ["support"]},
    ("GET", "/policy-roles"): {"roles": {"support": {"*": ["refunds"]}}},
}
# One call of every tool here, each with the arguments it needs.
CALLS = [
    ("policy_list_files", {}),
    ("policy_read_file", {"name": "policy.yaml"}),
    ("policy_resolve", {}),
    ("policy_graph", {}),
    ("policy_list_roles", {}),
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
    """A request as (method, path, query)."""
    path = request.url.path.removeprefix(BASE)
    return (request.method, path, dict(request.url.params))


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
                "modules": [{"tier": "capability", "path": "refunds", **STAMP}],
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
                ("GET", "/policy/resolve", {"role": "support", "agent": "billing"}),
            ],
            {"roles": {}},
            id="resolve-role-and-agent",
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
            [("GET", "/policy-roles", {})],
            ANSWERS["GET", "/policy-roles"],
            id="list_roles",
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


def test_when_role_is_unknown_then_policy_graph_refuses(token):
    mock_api()

    message = error(call(token, "policy_graph", role="Support"))

    assert message.endswith("role 'Support' not defined (known: ['support'])")


@pytest.mark.parametrize("name", ["policy_resolve", "policy_graph"])
def test_when_project_has_no_policy_store_then_resolved_views_refuse(token, name):
    mock_api(**{"/policy-files": [CAP], "/policy-roles": {"roles": {}}})

    assert "each agent enforces its own policy" in error(call(token, name))
    assert [r.url.path for r in api_requests()][-1] == f"{BASE}/policy-roles"


@pytest.mark.parametrize("name", ["policy_resolve", "policy_graph"])
def test_when_project_has_role_bindings_only_then_resolved_views_answer(token, name):
    mock_api(**{"/policy-files": []})

    assert not call(token, name)["isError"]
    path = {"policy_resolve": "/policy/resolve", "policy_graph": "/policy/graph"}
    assert sent(api_requests()[-1])[1] == path[name]


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
