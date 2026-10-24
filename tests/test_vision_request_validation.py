import base64
import io
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image

from backend.vision.body_limit import MAX_VISION_REQUEST_BODY_BYTES, VisionRequestBodyLimitMiddleware
from backend.vision.contracts import (
    VisionAutomationBinding,
    VisionAutomationTransportMetadata,
)
from backend.vision.validation import (
    MAX_ENCODED_DATA_URL_BYTES,
    VisionRequestValidationError,
    redact_data_urls,
    validate_agent_vision_request,
    validate_chat_vision_request,
    validate_encoded_data_url_budget,
)


def test_vision_budget_constants_allow_the_128_mib_payload_and_136_mib_request_overhead():
    assert MAX_ENCODED_DATA_URL_BYTES == 134_217_728
    assert MAX_VISION_REQUEST_BODY_BYTES == 142_606_336


def _config(*, encoded_limit: int = 134_217_728):
    return SimpleNamespace(
        vision_automation=SimpleNamespace(max_encoded_payload_bytes=encoded_limit),
        model=SimpleNamespace(
            max_image_size=10 * 1024 * 1024,
            max_image_dimension=1280,
            supported_formats="png,jpg,jpeg,gif,webp",
        ),
    )


def _data_url(*, image_format: str = "PNG", declared_mime: str | None = None) -> str:
    image = Image.new("RGB", (2, 2), color="red")
    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    mime = declared_mime or f"image/{image_format.lower()}"
    return f"data:{mime};base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"


def _truncated_data_url(image_format: str) -> str:
    image = Image.new("RGB", (32, 32), color="red")
    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    payload = buffer.getvalue()
    truncated = payload[: max(1, len(payload) // 2)]
    mime = "jpeg" if image_format == "JPEG" else image_format.lower()
    return f"data:image/{mime};base64,{base64.b64encode(truncated).decode('ascii')}"


def _missing_terminal_data_url(image_format: str) -> str:
    image = Image.new("RGB", (32, 32), color="red")
    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    payload = buffer.getvalue()
    terminal_lengths = {"JPEG": 2, "PNG": 12, "GIF": 1, "WEBP": 1}
    incomplete = payload[: -terminal_lengths[image_format]]
    mime = "jpeg" if image_format == "JPEG" else image_format.lower()
    return f"data:image/{mime};base64,{base64.b64encode(incomplete).decode('ascii')}"


def _chat_messages(images: list[str]):
    return [
        SimpleNamespace(
            role="user",
            content=[
                *[
                    {"type": "image_url", "image_url": {"url": image}}
                    for image in images
                ],
                {"type": "text", "text": "inspect"},
            ],
        )
    ]


def _automation_binding():
    return VisionAutomationBinding(
        page_generation=1,
        mode="agent",
        top_level_context_id="agent-session-0001",
        automation_task_id="task_01J0123456789ABCDEFGH",
        capture_cycle_id="cycle_01J0123456789ABCDEFG",
    )


@pytest.mark.parametrize(
    ("image", "reason"),
    [
        ("data:image/png;base64,%%%", "base64"),
        (_data_url(image_format="PNG", declared_mime="image/jpeg"), "MIME"),
        (
            "data:image/png;base64," + base64.b64encode(b"not an image").decode(),
            "image",
        ),
    ],
)
def test_chat_and_agent_reject_bad_images_with_same_indexed_diagnostic(image, reason):
    for validator, value in (
        (validate_chat_vision_request, _chat_messages([_data_url(), image])),
        (validate_agent_vision_request, [_data_url(), image]),
    ):
        with pytest.raises(VisionRequestValidationError) as exc_info:
            validator(value, _config())

        assert exc_info.value.status_code == 422
        assert exc_info.value.image_index == 2
        assert reason.lower() in str(exc_info.value).lower()
        assert "data:image" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("validator", "builder"),
    [
        (validate_chat_vision_request, _chat_messages),
        (validate_agent_vision_request, lambda images: images),
    ],
)
def test_chat_and_agent_accept_250_images_and_reject_251(validator, builder):
    image = _data_url()

    accepted = validator(builder([image] * 250), _config())
    assert accepted.image_count == 250

    with pytest.raises(VisionRequestValidationError) as exc_info:
        validator(builder([image] * 251), _config())
    assert exc_info.value.status_code == 422


def test_encoded_data_url_budget_accepts_exact_limit_and_rejects_one_byte_over():
    validate_encoded_data_url_budget(134_217_728, 134_217_728)

    with pytest.raises(VisionRequestValidationError) as exc_info:
        validate_encoded_data_url_budget(134_217_729, 134_217_728)

    assert exc_info.value.status_code == 413


@pytest.mark.parametrize(
    "metadata",
    [
        VisionAutomationTransportMetadata(
            history_image_policy="aggregate_prior_image_blocks",
            manual_image_count=10,
            source_count=1,
            retained_tick_count=1,
            binding=_automation_binding(),
        ),
        VisionAutomationTransportMetadata(
            history_image_policy="aggregate_prior_image_blocks",
            manual_image_count=0,
            source_count=242,
            retained_tick_count=1,
            binding=_automation_binding(),
        ),
        VisionAutomationTransportMetadata(
            history_image_policy="aggregate_prior_image_blocks",
            manual_image_count=1,
            source_count=2,
            retained_tick_count=2,
            binding=_automation_binding(),
        ),
    ],
)
def test_automation_metadata_rejects_manual_source_and_formula_forgery(metadata):
    images = [_data_url()] * 4
    with pytest.raises(VisionRequestValidationError) as exc_info:
        validate_agent_vision_request(images, _config(), metadata=metadata)
    assert exc_info.value.status_code == 422


def test_normal_vision_without_automation_metadata_remains_valid():
    result = validate_chat_vision_request(_chat_messages([_data_url()]), _config())
    assert result.image_count == 1
    assert result.encoded_data_url_bytes > 0


@pytest.mark.parametrize(
    "value",
    [
        "data:image/png;base64,AAAA",
        "bad data:image/png;base64,%%% payload",
    ],
)
def test_log_redaction_removes_valid_and_malformed_data_urls(value):
    redacted = redact_data_urls(value)
    assert "data:image" not in redacted
    assert "base64" not in redacted


def test_log_redaction_removes_malformed_data_url_suffix_conservatively():
    redacted = redact_data_urls(
        "failure data:image/png;base64,%%% leaked-suffix should-not-survive\n"
        "second-line-secret"
    )

    assert "data:image" not in redacted
    assert "leaked-suffix" not in redacted
    assert "should-not-survive" not in redacted
    assert "second-line-secret" not in redacted


def test_agent_image_limit_includes_existing_top_level_logical_prompt_images():
    with pytest.raises(VisionRequestValidationError) as exc_info:
        validate_agent_vision_request(
            [_data_url()],
            _config(),
            historical_image_count=250,
        )

    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "GIF", "WEBP"])
def test_strict_image_validation_rejects_truncated_supported_formats(image_format):
    with pytest.raises(VisionRequestValidationError):
        validate_agent_vision_request([_truncated_data_url(image_format)], _config())


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "GIF", "WEBP"])
def test_strict_image_validation_requires_format_terminal_record(image_format):
    with pytest.raises(VisionRequestValidationError):
        validate_agent_vision_request(
            [_missing_terminal_data_url(image_format)], _config()
        )


@pytest.mark.parametrize(
    "path",
    ["/v1/chat/completions", "/v1/agent/chat", "/v1/responses"],
)
def test_body_limit_accepts_exact_limit_and_rejects_declared_byte_over(path):
    app = FastAPI()
    app.add_middleware(VisionRequestBodyLimitMiddleware, max_body_bytes=10)

    @app.post(path)
    async def endpoint(request: Request):
        return {"size": len(await request.body())}

    client = TestClient(app)
    assert client.post(path, content=b"x" * 10).status_code == 200
    response = client.post(path, content=b"x" * 11)
    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


@pytest.mark.asyncio
async def test_body_limit_counts_actual_chunked_receive_bytes_without_content_length():
    endpoint_called = False

    async def endpoint(scope, receive, send):
        nonlocal endpoint_called
        endpoint_called = True
        while (await receive()).get("more_body"):
            pass

    middleware = VisionRequestBodyLimitMiddleware(endpoint, max_body_bytes=10)
    messages = iter(
        [
            {"type": "http.request", "body": b"x" * 6, "more_body": True},
            {"type": "http.request", "body": b"x" * 5, "more_body": False},
        ]
    )
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
        send,
    )

    assert endpoint_called is True
    assert sent[0]["status"] == 413


def test_body_limit_does_not_apply_to_unrelated_routes():
    app = FastAPI()
    app.add_middleware(VisionRequestBodyLimitMiddleware, max_body_bytes=10)

    @app.post("/other")
    async def endpoint(request: Request):
        return {"size": len(await request.body())}

    response = TestClient(app).post("/other", content=b"x" * 11)
    assert response.status_code == 200
