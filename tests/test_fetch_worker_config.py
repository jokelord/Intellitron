"""Worker limits must survive YAML parsing without accepting unsafe values."""

import pytest

from backend.config import ConfigLoader


def _parse(data):
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {"web_tools": data}
    loader._agent_prompts_data = {}
    return loader._parse_web_tools_config()


@pytest.mark.parametrize("key,default", [
    ("fetch_crawl4ai_max_concurrency", 50),
    ("fetch_worker_processes", 4),
])
@pytest.mark.parametrize("value", [None, 0, -1, "bad", True, False, 1.5, float("nan"), float("inf")])
def test_invalid_worker_counts_use_defaults(key, default, value):
    assert getattr(_parse({key: value}), key, None) == default


@pytest.mark.parametrize("key,default", [
    ("fetch_worker_cancel_grace_seconds", 1.0),
    ("crawl4ai_memory_threshold_percent", 85.0),
    ("crawl4ai_memory_check_interval_seconds", 0.1),
    ("crawl4ai_batch_wait_seconds", 0.01),
])
@pytest.mark.parametrize("value", [None, -1, "bad", True, False, float("nan"), float("inf"), "-inf"])
def test_invalid_worker_float_values_use_defaults(key, default, value):
    assert getattr(_parse({key: value}), key, None) == default


@pytest.mark.parametrize("key,default", [
    ("fetch_worker_cancel_grace_seconds", 1.0),
    ("crawl4ai_memory_check_interval_seconds", 0.1),
    ("crawl4ai_memory_threshold_percent", 85.0),
])
def test_required_positive_values_reject_zero(key, default):
    assert getattr(_parse({key: 0}), key, None) == default


@pytest.mark.parametrize("value", [100, 101])
def test_memory_threshold_must_be_below_one_hundred(value):
    assert getattr(_parse({"crawl4ai_memory_threshold_percent": value}), "crawl4ai_memory_threshold_percent", None) == 85.0


def test_worker_settings_parse_numeric_strings_and_allow_no_batch_delay():
    values = {
        "fetch_crawl4ai_max_concurrency": "60",
        "fetch_worker_processes": "6",
        "fetch_worker_cancel_grace_seconds": "2.5",
        "crawl4ai_memory_threshold_percent": "90.5",
        "crawl4ai_memory_check_interval_seconds": "0.2",
        "crawl4ai_batch_wait_seconds": 0,
    }
    cfg = _parse(values)
    assert cfg.fetch_crawl4ai_max_concurrency == 60
    assert getattr(cfg, "fetch_worker_processes", None) == 6
    assert getattr(cfg, "fetch_worker_cancel_grace_seconds", None) == 2.5
    assert getattr(cfg, "crawl4ai_memory_threshold_percent", None) == 90.5
    assert getattr(cfg, "crawl4ai_memory_check_interval_seconds", None) == 0.2
    assert getattr(cfg, "crawl4ai_batch_wait_seconds", None) == 0.0
