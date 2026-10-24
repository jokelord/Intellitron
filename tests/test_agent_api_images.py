from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.agent import router as agent_router


def test_non_stream_agent_chat_forwards_images_to_run_sync():
    app = FastAPI()
    app.include_router(agent_router)

    mock_runner = MagicMock()
    mock_runner.run_sync = AsyncMock(
        return_value={"session_id": "s1", "answer": "ok", "steps": []}
    )
    app.state.agent_runner = mock_runner

    client = TestClient(app)
    resp = client.post(
        "/v1/agent/chat",
        json={
            "session_id": "s1",
            "message": "图中有什么？",
            "images": ["data:image/jpeg;base64,abc"],
            "stream": False,
        },
    )

    assert resp.status_code == 200
    mock_runner.run_sync.assert_awaited_once_with(
        "s1",
        "图中有什么？",
        None,
        images=["data:image/jpeg;base64,abc"],
        context_messages=None,
        enable_thinking=None,
    )


def test_stream_agent_chat_forwards_images_to_run_stream():
    app = FastAPI()
    app.include_router(agent_router)

    async def _stream_events():
        yield 'data: {"type": "session_start", "session_id": "s1"}\n\n'
        yield 'data: {"type": "done", "session_id": "s1"}\n\n'

    mock_runner = MagicMock()
    mock_runner.run_stream.return_value = _stream_events()
    app.state.agent_runner = mock_runner

    client = TestClient(app)
    with client.stream(
        "POST",
        "/v1/agent/chat",
        json={
            "session_id": "s1",
            "message": "图中有什么？",
            "images": ["data:image/jpeg;base64,abc"],
            "stream": True,
        },
    ) as resp:
        body = "".join(resp.iter_text())

    assert resp.status_code == 200
    assert '"type": "session_start"' in body
    assert '"type": "done"' in body
    mock_runner.run_stream.assert_called_once_with(
        "s1",
        "图中有什么？",
        None,
        images=["data:image/jpeg;base64,abc"],
        context_messages=None,
        enable_thinking=None,
    )


def test_stream_agent_chat_forwards_enable_thinking_to_run_stream():
    app = FastAPI()
    app.include_router(agent_router)

    async def _stream_events():
        yield 'data: {"type": "done", "session_id": "s1"}\n\n'

    mock_runner = MagicMock()
    mock_runner.run_stream.return_value = _stream_events()
    app.state.agent_runner = mock_runner

    client = TestClient(app)
    with client.stream(
        "POST",
        "/v1/agent/chat",
        json={
            "session_id": "s1",
            "message": "请深入分析",
            "stream": True,
            "enable_thinking": True,
        },
    ) as resp:
        body = "".join(resp.iter_text())

    assert resp.status_code == 200
    assert '"type": "done"' in body
    mock_runner.run_stream.assert_called_once_with(
        "s1",
        "请深入分析",
        None,
        images=None,
        context_messages=None,
        enable_thinking=True,
    )


def test_non_stream_agent_chat_forwards_enable_thinking_to_run_sync():
    app = FastAPI()
    app.include_router(agent_router)

    mock_runner = MagicMock()
    mock_runner.run_sync = AsyncMock(
        return_value={"session_id": "s1", "answer": "ok", "steps": []}
    )
    app.state.agent_runner = mock_runner

    client = TestClient(app)
    resp = client.post(
        "/v1/agent/chat",
        json={
            "session_id": "s1",
            "message": "请深入分析",
            "stream": False,
            "enable_thinking": True,
        },
    )

    assert resp.status_code == 200
    mock_runner.run_sync.assert_awaited_once_with(
        "s1",
        "请深入分析",
        None,
        images=None,
        context_messages=None,
        enable_thinking=True,
    )


def test_stream_agent_chat_returns_409_when_session_is_busy():
    app = FastAPI()
    app.include_router(agent_router)

    mock_runner = MagicMock()
    mock_runner.get_or_create_session.side_effect = RuntimeError(
        "Session busy-session already has an active run"
    )
    app.state.agent_runner = mock_runner

    client = TestClient(app)
    response = client.post(
        "/v1/agent/chat",
        json={
            "session_id": "busy-session",
            "message": "图中有什么？",
            "stream": True,
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Session busy-session already has an active run"}
    mock_runner.run_stream.assert_not_called()


def test_stream_agent_chat_without_session_id_skips_preflight_session_lookup():
    app = FastAPI()
    app.include_router(agent_router)

    async def _stream_events():
        yield 'data: {"type": "session_start", "session_id": "generated-session"}\n\n'
        yield 'data: {"type": "done", "session_id": "generated-session"}\n\n'

    mock_runner = MagicMock()
    mock_runner.run_stream.return_value = _stream_events()
    app.state.agent_runner = mock_runner

    client = TestClient(app)
    with client.stream(
        "POST",
        "/v1/agent/chat",
        json={
            "message": "hello",
            "stream": True,
        },
    ) as resp:
        body = "".join(resp.iter_text())

    assert resp.status_code == 200
    assert '"session_id": "generated-session"' in body
    mock_runner.get_or_create_session.assert_not_called()
    mock_runner.run_stream.assert_called_once_with(
        None,
        "hello",
        None,
        images=None,
        context_messages=None,
        enable_thinking=None,
    )
