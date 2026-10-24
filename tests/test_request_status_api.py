import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.request_status import router as request_status_router
from backend.request_lifecycle import RequestLifecycleRegistry


def _app_with_registry(registry: RequestLifecycleRegistry) -> FastAPI:
    app = FastAPI()
    app.include_router(request_status_router)
    app.state.request_lifecycle_registry = registry
    return app


def test_request_status_endpoint_reports_unknown():
    response = TestClient(_app_with_registry(RequestLifecycleRegistry())).get(
        "/v1/request-status/missing-request"
    )

    assert response.status_code == 200
    assert response.json() == {
        "request_id": "missing-request",
        "status": "unknown",
    }


def test_request_status_endpoint_reports_lifecycle_without_prompt_data():
    registry = RequestLifecycleRegistry()
    reservation = registry.reserve(
        request_id="request-1",
        request_kind="agent",
        context_identity="agent-session-1",
        payload_fingerprint=hashlib.sha256(b"sensitive payload").hexdigest(),
    )
    token = reservation.registration_token
    assert token is not None
    client = TestClient(_app_with_registry(registry))

    registry.accept(token)
    accepted = client.get("/v1/request-status/request-1")
    registry.mark_running(token)
    running = client.get("/v1/request-status/request-1")
    registry.mark_terminal(token, "completed")
    terminal = client.get("/v1/request-status/request-1")

    assert accepted.json() == {"request_id": "request-1", "status": "accepted"}
    assert running.json() == {"request_id": "request-1", "status": "running"}
    assert terminal.json() == {"request_id": "request-1", "status": "terminal"}
    assert "fingerprint" not in terminal.text
    assert "agent-session-1" not in terminal.text
    assert "completed" not in terminal.text


def test_request_status_endpoint_disables_caching():
    response = TestClient(_app_with_registry(RequestLifecycleRegistry())).get(
        "/v1/request-status/missing-request"
    )

    assert response.headers["cache-control"] == "no-store"
