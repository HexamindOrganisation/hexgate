from starlette.testclient import TestClient

from hexgate_assistant.server import create_app


def test_health_happy_path():
    response = TestClient(create_app()).get("/v1/assistant/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "hexgate-assistant"}


def test_when_path_is_not_the_health_route_then_404():
    assert TestClient(create_app()).get("/health").status_code == 404
