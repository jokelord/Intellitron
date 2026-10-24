"""Tests for preserving multimodal request content before inference."""

import base64
import io

from PIL import Image

from backend.llm_service import Message, prepare_multimodal_messages


def make_data_url(width: int, height: int) -> str:
    image = Image.new("RGB", (width, height), color=(255, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return f"data:image/jpeg;base64,{base64.b64encode(buffer.getvalue()).decode()}"


def test_prepare_multimodal_messages_preserves_historical_images_and_input():
    old_image = make_data_url(4, 4)
    current_image = make_data_url(4, 4)
    original = [
        Message(role="user", content=[
            {"type": "image_url", "image_url": {"url": old_image}},
            {"type": "text", "text": "first image"},
        ]),
        Message(role="assistant", content="I see it."),
        Message(role="user", content=[
            {"type": "image_url", "image_url": {"url": current_image}},
            {"type": "text", "text": "second image"},
        ]),
    ]

    prepared = prepare_multimodal_messages(original, max_image_dimension=1280)

    assert prepared is not original
    assert prepared[0].content[0]["type"] == "image_url"
    assert prepared[0].content[0]["image_url"]["url"] == old_image
    assert prepared[2].content[0]["image_url"]["url"] == current_image
    assert original[0].content[0]["image_url"]["url"] == old_image
    assert prepared[0] is not original[0]
    assert prepared[0].content is not original[0].content
    assert prepared[0].content[0] is not original[0].content[0]
    assert prepared[0].content[0]["image_url"] is not original[0].content[0]["image_url"]

    prepared[0].content[0]["image_url"]["url"] = "data:image/jpeg;base64,changed"

    assert original[0].content[0]["image_url"]["url"] == old_image


def test_prepare_full_second_turn_history_is_deterministic_and_preserves_input():
    historical_image = make_data_url(20, 10)
    original = [
        Message(role="user", content=[
            {"type": "image_url", "image_url": {"url": historical_image}},
            {"type": "text", "text": "first turn"},
        ]),
        Message(role="assistant", content="I see it."),
        Message(role="user", content="second turn"),
    ]

    first_prepared = prepare_multimodal_messages(original, max_image_dimension=8)
    second_prepared = prepare_multimodal_messages(original, max_image_dimension=8)

    first_image_part = first_prepared[0].content[0]
    second_image_part = second_prepared[0].content[0]
    assert first_prepared == second_prepared
    assert first_image_part["type"] == "image_url"
    assert first_image_part["image_url"]["url"] == second_image_part["image_url"]["url"]
    assert first_image_part["image_url"]["url"] != historical_image
    assert original[0].content[0]["image_url"]["url"] == historical_image


def test_prepare_multimodal_messages_resizes_only_valid_oversized_data_urls():
    source_image = make_data_url(20, 10)
    remote_image = "https://example.test/image.jpg"
    invalid_image = "data:image/jpeg;base64,not-base64"
    original = [
        Message(role="user", content=[
            {"type": "image_url", "image_url": {"url": source_image}},
            {"type": "image_url", "image_url": {"url": remote_image}},
            {"type": "image_url", "image_url": {"url": invalid_image}},
        ]),
    ]

    prepared = prepare_multimodal_messages(original, max_image_dimension=8)

    resized_url = prepared[0].content[0]["image_url"]["url"]
    encoded_image = resized_url.split(",", 1)[1]
    with Image.open(io.BytesIO(base64.b64decode(encoded_image))) as image:
        assert image.size == (8, 4)
    assert prepared[0].content[1]["image_url"]["url"] == remote_image
    assert prepared[0].content[2]["image_url"]["url"] == invalid_image
    assert original[0].content[0]["image_url"]["url"] == source_image


def test_prepare_multimodal_messages_preserves_non_image_and_malformed_data_headers():
    encoded_image = make_data_url(20, 10).split(",", 1)[1]
    non_image_url = f"data:text/plain;base64,{encoded_image}"
    malformed_image_url = f"data:image/jpeg,{encoded_image}"
    original = [
        Message(role="user", content=[
            {"type": "image_url", "image_url": {"url": non_image_url}},
            {"type": "image_url", "image_url": {"url": malformed_image_url}},
        ]),
    ]

    prepared = prepare_multimodal_messages(original, max_image_dimension=8)

    assert prepared[0].content[0]["image_url"]["url"] == non_image_url
    assert prepared[0].content[1]["image_url"]["url"] == malformed_image_url


def test_only_trusted_computer_use_image_keeps_full_resolution_and_lossless_png():
    from smolagents.models import ChatMessage, MessageRole, get_clean_message_list, tool_role_conversions

    from backend.agent.computer_use_history import mark_computer_use_observation
    from backend.agent.local_llm_model import LocalLLMModel

    desktop = mark_computer_use_observation(
        Image.new("RGB", (32, 20), (17, 89, 231)),
        context_id="agent-1", observation_id="obs-1", geometry_version=1,
    )
    upload = Image.new("RGB", (32, 20), (17, 89, 231))
    messages = [ChatMessage(role=MessageRole.USER, content=[
        {"type": "image", "image": desktop},
        {"type": "image", "image": upload},
    ])]
    encoded = LocalLLMModel._pre_encode_images(messages, trusted_context_id="agent-1")
    clean = get_clean_message_list(encoded, role_conversions=tool_role_conversions,
                                   convert_images_to_image_urls=True)
    protected = LocalLLMModel._extract_trusted_desktop_urls(clean)
    assert len(protected) == 1
    assert all("_trusted_desktop_original" not in item for item in clean[0]["content"])
    urls = [part["image_url"]["url"] for part in clean[0]["content"]]
    prepared = prepare_multimodal_messages(
        [Message(role="user", content=clean[0]["content"])],
        max_image_dimension=8, preserve_image_urls=protected,
    )
    kept, resized = [part["image_url"]["url"] for part in prepared[0].content]
    assert kept == urls[0] and kept.startswith("data:image/png;base64,")
    assert resized != urls[1] and resized.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(base64.b64decode(kept.partition(",")[2]))) as image:
        assert image.size == (32, 20)
        assert image.getpixel((3, 3)) == (17, 89, 231)
