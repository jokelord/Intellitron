import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.api.chat import ChatStopRequest, stop_chat


def test_chat_stop_reports_targeted_provider_abort_failure():
    with (
        patch("backend.config.get_config") as mock_get_config,
        patch("backend.llm_service.abort_request_by_id", return_value=False),
    ):
        mock_get_config.return_value = MagicMock(
            sglang=MagicMock(host="127.0.0.1", port=30000)
        )
        with pytest.raises(HTTPException) as caught:
            asyncio.run(stop_chat(ChatStopRequest(request_id="req-chat-failed-stop")))

    assert caught.value.status_code == 502
