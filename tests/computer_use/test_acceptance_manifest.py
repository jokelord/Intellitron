from scripts.benchmark_computer_use import acceptance_ready


def test_mock_results_cannot_mark_platform_ready():
    assert not acceptance_ready({"geometry": "pass", "mock_input": "pass", "live_wayland": "not_run"})
