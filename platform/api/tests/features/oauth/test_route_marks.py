"""Default-deny, by enumeration: only the routes in the scope registry accept an OAuth token.

Every HTTP route the app registers at import is called with a token holding
every scope and naming the default project, so a refusal can only come from
the route's mark, never from the token. Not enumerated, none behind
require_user: websocket routes, which authenticate with their own cookie-only
dependencies; plain Starlette routes (the OpenAPI docs, the MCP SDK's
``/v1/oauth/authorize`` and ``/v1/oauth/token``), which carry no
``openapi_extra`` for a mark; and the routes the lifespan mounts (Google
login, demo login, the SPA).
"""

from __future__ import annotations

import re

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from hexgate_api.constants import (
    DEFAULT_ORG_ID,
    DEFAULT_PROJECT_ID,
    OAUTH_NO_PROJECT,
    OAUTH_SCOPE_KEY,
    OAUTH_SCOPES,
)
from hexgate_api.deps.identity import require_user
from hexgate_api.main import app
from tests.features.oauth.conftest import mint

_P = "/v1/projects/{project_id}"
_READ = {OAUTH_SCOPE_KEY: "policy:read"}
# The two routes a client finds a project_id with, so they have none to check.
_FIND = {OAUTH_SCOPE_KEY: "projects:read", OAUTH_NO_PROJECT: True}
# The scope registry's routes, verbatim. Widening it is a visible diff here.
REGISTRY = {
    ("GET", f"{_P}/policy-modules"): _READ,
    ("GET", f"{_P}/policy-roles"): _READ,
    ("GET", f"{_P}/policy-files"): _READ,
    ("POST", f"{_P}/policy/preview"): _READ,
    ("GET", f"{_P}/policy/resolve"): _READ,
    ("GET", f"{_P}/policy/check"): _READ,
    ("GET", f"{_P}/policy/graph"): _READ,
    ("POST", f"{_P}/policy/test"): _READ,
    ("PUT", f"{_P}/policy-files/{{name:path}}"): {OAUTH_SCOPE_KEY: "policy:write"},
    ("GET", f"{_P}/audit/decisions"): {OAUTH_SCOPE_KEY: "audit:read"},
    ("GET", f"{_P}/agents/manifest"): {OAUTH_SCOPE_KEY: "agents:read"},
    ("GET", _P): {OAUTH_SCOPE_KEY: "projects:read"},
    ("GET", "/v1/orgs"): _FIND,
    ("GET", "/v1/orgs/{org_id}/projects"): _FIND,
}

_SCOPE_REFUSED = "token scope does not cover this route"
_PATH_PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")
_PARAM_VALUES = {"project_id": DEFAULT_PROJECT_ID, "org_id": DEFAULT_ORG_ID}


def _endpoints() -> list[tuple[str, APIRoute]]:
    return [
        (method, route)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods)
    ]


def _marks(route: APIRoute) -> dict:
    extra = route.openapi_extra or {}
    return {k: extra[k] for k in (OAUTH_SCOPE_KEY, OAUTH_NO_PROJECT) if k in extra}


def _depends_on_require_user(dependant: Dependant) -> bool:
    return any(
        d.call is require_user or _depends_on_require_user(d)
        for d in dependant.dependencies
    )


def _url(route: APIRoute) -> str:
    return _PATH_PARAM.sub(lambda m: _PARAM_VALUES.get(m[1], "x"), route.path)


def _params(endpoints, keep):
    return [
        pytest.param(method, route, id=f"{method} {route.path}")
        for method, route in endpoints
        if keep(route)
    ]


ENDPOINTS = _endpoints()
MARKED = _params(ENDPOINTS, lambda r: bool(_marks(r)))
UNMARKED_USER = _params(
    ENDPOINTS, lambda r: not _marks(r) and _depends_on_require_user(r.dependant)
)
UNMARKED_OTHER = _params(
    ENDPOINTS, lambda r: not _marks(r) and not _depends_on_require_user(r.dependant)
)


@pytest.fixture
def token() -> str:
    return mint(scopes=sorted(OAUTH_SCOPES))


def _call(client: TestClient, method: str, route: APIRoute, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.request(method, _url(route), headers=headers)


def test_when_routes_are_enumerated_then_the_marked_set_equals_the_registry() -> None:
    marked = [((m, r.path), _marks(r)) for m, r in ENDPOINTS if _marks(r)]
    # A list, not a dict: a second route on the same method and path would
    # otherwise overwrite the first and hide its mark.
    assert len(marked) == len(REGISTRY)
    assert dict(marked) == REGISTRY


@pytest.mark.parametrize(("method", "route"), MARKED)
def test_when_route_is_marked_then_it_names_registered_scopes_and_a_project(
    method, route
) -> None:
    """A typo'd scope would be satisfiable by no token; a missing project check by any."""
    marks = _marks(route)
    needed = marks.get(OAUTH_SCOPE_KEY)
    scopes = [needed] if isinstance(needed, str) else needed
    assert isinstance(scopes, list) and scopes, marks
    assert all(isinstance(s, str) and s in OAUTH_SCOPES for s in scopes), marks
    assert marks.get(OAUTH_NO_PROJECT, True) is True, marks
    assert "project_id" in route.param_convertors or OAUTH_NO_PROJECT in marks
    assert _depends_on_require_user(route.dependant), "a mark nothing reads"


@pytest.mark.parametrize(("method", "route"), MARKED)
def test_when_route_is_marked_then_require_user_accepts_the_token(
    client: TestClient, token: str, method, route
) -> None:
    """The token passes ``require_user`` and the route's own gate (the user owns the project)."""
    r = _call(client, method, route, token)
    # 422: no body was sent, and FastAPI validates it only after the
    # dependencies passed. 503: the audit route with an unreachable or broken ClickHouse.
    assert r.status_code < 400 or r.status_code in (422, 503), r.text


@pytest.mark.parametrize(("method", "route"), UNMARKED_USER)
def test_when_route_is_unmarked_then_an_all_scopes_token_is_refused(
    client: TestClient, token: str, method, route
) -> None:
    r = _call(client, method, route, token)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == _SCOPE_REFUSED


@pytest.mark.parametrize(("method", "route"), UNMARKED_OTHER)
def test_when_route_skips_require_user_then_the_token_changes_nothing(
    client: TestClient, token: str, method, route
) -> None:
    """SDK, OTLP and public routes answer a token exactly as they answer no auth."""
    with_token = _call(client, method, route, token)
    without = _call(client, method, route, None)
    assert with_token.status_code == without.status_code, with_token.text
