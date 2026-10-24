from dataclasses import replace

from fastapi.testclient import TestClient

from backend.config import ConfigLoader, WebUIConfig
import backend.main as main_mod


def test_config_returns_raw_web_ui_image_paths(monkeypatch, tmp_path):
    user_image = tmp_path / "me.jpg"
    assistant_image = tmp_path / "assistant.jpg"
    welcome_image = tmp_path / "welcome.webp"
    user_image.write_bytes(b"user-image")
    assistant_image.write_bytes(b"assistant-image")
    welcome_image.write_bytes(b"welcome-image")

    current = ConfigLoader().load(reload=True)
    replacement = replace(
        current,
        web_ui=WebUIConfig(
            message_avatar={
                "user_image_path": str(user_image),
                "assistant_image_path": str(assistant_image),
                "size_px": 32,
            },
            vision_welcome={
                "title": "欢迎",
                "description": "说明",
                "icon_image_path": str(welcome_image),
            },
        ),
    )
    monkeypatch.setattr(main_mod, "reload_config", lambda: replacement)

    client = TestClient(main_mod.app)
    response = client.get("/config")

    assert response.status_code == 200
    components = response.json()["webUI"]["components"]
    user_url = components["message_avatar"]["user_image_path"]
    assistant_url = components["message_avatar"]["assistant_image_path"]
    welcome_url = components["vision_welcome"]["icon_image_path"]
    assert user_url == str(user_image)
    assert assistant_url == str(assistant_image)
    assert welcome_url == str(welcome_image)


def test_web_ui_asset_route_rejects_unconfigured_ids():
    client = TestClient(main_mod.app)

    response = client.get("/v1/webui/assets/not-configured")

    assert response.status_code == 404
