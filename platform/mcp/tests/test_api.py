import httpx
import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError

from hexgate_mcp.api import ApiClient
from tests.conftest import SETTINGS

ORGS_URL = f"{SETTINGS.api_v1_url}/orgs"


def api() -> ApiClient:
    return ApiClient(SETTINGS.api_v1_url, httpx.AsyncClient())


@respx.mock
async def test_get_happy_path():
    route = respx.get(ORGS_URL).respond(json=[{"id": "org-1"}])

    assert await api().get("/orgs", "the-token") == [{"id": "org-1"}]
    assert route.calls.last.request.headers["authorization"] == "Bearer the-token"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "401 not authenticated: token expired"),
        (403, "403 forbidden: token expired"),
        (404, "404 not found: token expired"),
        (409, "409 conflict: token expired"),
        (422, "422 invalid request: token expired"),
    ],
)
@respx.mock
async def test_when_api_refuses_then_tool_error_carries_detail(status, expected):
    respx.get(ORGS_URL).respond(status, json={"detail": "token expired"})

    with pytest.raises(ToolError) as exc:
        await api().get("/orgs", "t")

    assert str(exc.value) == expected


@respx.mock
async def test_when_422_detail_is_a_list_then_tool_error_carries_it_as_json():
    errors = [{"loc": ["query", "limit"], "msg": "too large"}]
    respx.get(ORGS_URL).respond(422, json={"detail": errors})

    with pytest.raises(ToolError, match=r'"msg": "too large"'):
        await api().get("/orgs", "t")


@respx.mock
async def test_when_refusal_body_is_long_and_not_json_then_it_is_cut():
    respx.get(ORGS_URL).respond(403, text="<html>" + "x" * 5000)

    with pytest.raises(ToolError) as exc:
        await api().get("/orgs", "t")

    assert str(exc.value) == "403 forbidden: " + ("<html>" + "x" * 5000)[:300]


@respx.mock
async def test_when_refusal_is_not_json_then_tool_error_carries_body():
    respx.get(ORGS_URL).respond(404, text="no such route")

    with pytest.raises(ToolError, match="404 not found: no such route"):
        await api().get("/orgs", "t")


@pytest.mark.parametrize(
    ("response", "cause"),
    [
        (httpx.Response(500, json={"detail": "boom"}), "500"),
        (httpx.Response(307, headers={"Location": "/elsewhere"}), "307"),
        (httpx.Response(200, text="<html>"), "response is not JSON"),
        (httpx.ConnectError("refused"), "ConnectError"),
        (httpx.ReadTimeout(""), "ReadTimeout"),
    ],
)
@respx.mock
async def test_when_api_fails_then_logged_and_tool_error_hides_its_url(
    response, cause, caplog
):
    # A one-item side effect: respx returns a response and raises an error.
    respx.get(ORGS_URL).mock(side_effect=[response])

    with pytest.raises(ToolError) as exc:
        await api().get("/orgs", "the-token")

    assert str(exc.value) == "the Hexgate API failed to answer; try again shortly"
    assert caplog.messages == [f"API call failed: GET /orgs: {cause}"]
    assert caplog.records[0].levelname == "WARNING"


@respx.mock
async def test_when_a_param_is_none_then_it_is_left_out():
    route = respx.get(ORGS_URL).respond(json=[])

    await api().get("/orgs", "t", {"role": None, "agent": "*"})

    assert dict(route.calls.last.request.url.params) == {"agent": "*"}
