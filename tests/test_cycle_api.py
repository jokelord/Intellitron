"""
Tests for backend/api/cycle.py
"""

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI

from backend.api.cycle import router as cycle_router


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(cycle_router)

    mock_executor = MagicMock()
    mock_camera = MagicMock()

    app.state.cycle_executor = mock_executor
    app.state.camera_service = mock_camera

    return app, mock_executor, mock_camera


class TestCamerasEndpoint:
    """测试 GET /v1/cycle/cameras"""

    def test_list_cameras(self, app):
        app_instance, _, mock_camera = app
        mock_camera.list_cameras.return_value = [
            {"index": 0, "device": "/dev/video0"},
        ]
        client = TestClient(app_instance)
        resp = client.get("/v1/cycle/cameras")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["cameras"]) == 1
        assert data["cameras"][0]["index"] == 0

    def test_list_cameras_empty(self, app):
        app_instance, _, mock_camera = app
        mock_camera.list_cameras.return_value = []
        client = TestClient(app_instance)
        resp = client.get("/v1/cycle/cameras")
        assert resp.status_code == 200
        assert resp.json()["cameras"] == []

    def test_list_cameras_no_service(self):
        """camera_service 为 None 时返回空列表"""
        app = FastAPI()
        app.include_router(cycle_router)
        app.state.cycle_executor = MagicMock()
        app.state.camera_service = None
        client = TestClient(app)
        resp = client.get("/v1/cycle/cameras")
        assert resp.status_code == 200
        assert resp.json()["cameras"] == []


class TestStopEndpoint:
    """测试 POST /v1/cycle/stop"""

    def test_stop_running(self, app):
        app_instance, mock_executor, _ = app
        mock_executor.stop.return_value = True
        client = TestClient(app_instance)
        resp = client.post("/v1/cycle/stop", json={"session_id": "s1"})
        assert resp.status_code == 200
        assert resp.json()["stopped"] is True

    def test_stop_not_found(self, app):
        app_instance, mock_executor, _ = app
        mock_executor.stop.return_value = False
        client = TestClient(app_instance)
        resp = client.post("/v1/cycle/stop", json={"session_id": "s999"})
        assert resp.status_code == 200
        assert resp.json()["stopped"] is False

    def test_stop_no_executor(self):
        """executor 为 None 时返回 503"""
        app = FastAPI()
        app.include_router(cycle_router)
        app.state.cycle_executor = None
        app.state.camera_service = None
        client = TestClient(app)
        resp = client.post("/v1/cycle/stop", json={"session_id": "s1"})
        assert resp.status_code == 503
