"""
Tests for CycleConfig parsing in backend/config.py
"""

import pytest
from backend.config import (
    CycleStepConfig,
    CycleConfig,
    ConfigLoader,
    AppConfig,
)


class TestCycleStepConfig:
    """测试 CycleStepConfig 数据类"""

    def test_wait_step_defaults(self):
        step = CycleStepConfig(step_type="wait", wait_seconds=5.0)
        assert step.step_type == "wait"
        assert step.wait_seconds == 5.0
        assert step.message == ""
        assert step.photo == []
        assert step.cycle_count == 0
        assert step.cycle_steps == []

    def test_message_step_with_photo(self):
        step = CycleStepConfig(
            step_type="message",
            message="Hello",
            photo=[0, 1],
        )
        assert step.step_type == "message"
        assert step.message == "Hello"
        assert step.photo == [0, 1]

    def test_cycle_step_nested(self):
        inner = CycleStepConfig(step_type="wait", wait_seconds=2.0)
        outer = CycleStepConfig(
            step_type="cycle",
            cycle_count=3,
            cycle_steps=[inner],
        )
        assert outer.step_type == "cycle"
        assert outer.cycle_count == 3
        assert len(outer.cycle_steps) == 1
        assert outer.cycle_steps[0].step_type == "wait"


class TestCycleConfig:
    """测试 CycleConfig 数据类"""

    def test_defaults(self):
        cfg = CycleConfig()
        assert cfg.enabled is False
        assert cfg.max_depth == 5
        assert cfg.max_duration == 3600
        assert cfg.default_sequence == []

    def test_custom_values(self):
        step = CycleStepConfig(step_type="wait", wait_seconds=1.0)
        cfg = CycleConfig(
            enabled=True,
            max_depth=3,
            max_duration=600,
            default_sequence=[step],
        )
        assert cfg.enabled is True
        assert cfg.max_depth == 3
        assert len(cfg.default_sequence) == 1


class TestParseCycleConfig:
    """测试 ConfigLoader._parse_cycle_config()"""

    def _make_loader_with_data(self, cycle_data: dict) -> ConfigLoader:
        """创建一个带有预设数据的 ConfigLoader"""
        loader = ConfigLoader.__new__(ConfigLoader)
        loader._data = {"cycle": cycle_data}
        loader._agent_prompts_data = {}
        return loader

    def test_parse_empty_cycle(self):
        loader = self._make_loader_with_data({})
        cfg = loader._parse_cycle_config()
        assert isinstance(cfg, CycleConfig)
        assert cfg.enabled is False

    def test_parse_enabled(self):
        loader = self._make_loader_with_data({"enabled": True})
        cfg = loader._parse_cycle_config()
        assert cfg.enabled is True

    def test_parse_limits(self):
        loader = self._make_loader_with_data(
            {
                "max_depth": 3,
                "max_duration": 600,
            }
        )
        cfg = loader._parse_cycle_config()
        assert cfg.max_depth == 3
        assert cfg.max_duration == 600

    def test_parse_wait_step(self):
        loader = self._make_loader_with_data(
            {
                "default_sequence": [{"wait": 5}],
            }
        )
        cfg = loader._parse_cycle_config()
        assert len(cfg.default_sequence) == 1
        assert cfg.default_sequence[0].step_type == "wait"
        assert cfg.default_sequence[0].wait_seconds == 5.0

    def test_parse_message_step(self):
        loader = self._make_loader_with_data(
            {
                "default_sequence": [{"message": "Hello", "photo": [0, 1]}],
            }
        )
        cfg = loader._parse_cycle_config()
        step = cfg.default_sequence[0]
        assert step.step_type == "message"
        assert step.message == "Hello"
        assert step.photo == [0, 1]

    def test_parse_message_without_photo(self):
        loader = self._make_loader_with_data(
            {
                "default_sequence": [{"message": "Hi"}],
            }
        )
        cfg = loader._parse_cycle_config()
        step = cfg.default_sequence[0]
        assert step.step_type == "message"
        assert step.photo == []

    def test_parse_nested_cycle(self):
        loader = self._make_loader_with_data(
            {
                "default_sequence": [
                    {
                        "cycle": {
                            "count": 3,
                            "steps": [
                                {"wait": 2},
                                {"message": "Check"},
                            ],
                        }
                    }
                ],
            }
        )
        cfg = loader._parse_cycle_config()
        step = cfg.default_sequence[0]
        assert step.step_type == "cycle"
        assert step.cycle_count == 3
        assert len(step.cycle_steps) == 2
        assert step.cycle_steps[0].step_type == "wait"
        assert step.cycle_steps[1].step_type == "message"

    def test_parse_deeply_nested(self):
        loader = self._make_loader_with_data(
            {
                "default_sequence": [
                    {
                        "cycle": {
                            "count": 2,
                            "steps": [
                                {
                                    "cycle": {
                                        "count": 3,
                                        "steps": [{"wait": 1}],
                                    }
                                }
                            ],
                        }
                    }
                ],
            }
        )
        cfg = loader._parse_cycle_config()
        outer = cfg.default_sequence[0]
        assert outer.step_type == "cycle"
        inner = outer.cycle_steps[0]
        assert inner.step_type == "cycle"
        assert inner.cycle_count == 3
        assert inner.cycle_steps[0].step_type == "wait"

    def test_parse_full_sequence(self):
        loader = self._make_loader_with_data(
            {
                "enabled": True,
                "max_depth": 5,
                "max_duration": 3600,
                "default_sequence": [
                    {"wait": 5},
                    {"message": "Hello", "photo": [0]},
                    {
                        "cycle": {
                            "count": 3,
                            "steps": [
                                {"wait": 2},
                                {"message": "Iteration check"},
                            ],
                        }
                    },
                ],
            }
        )
        cfg = loader._parse_cycle_config()
        assert cfg.enabled is True
        assert len(cfg.default_sequence) == 3
        assert cfg.default_sequence[0].step_type == "wait"
        assert cfg.default_sequence[1].step_type == "message"
        assert cfg.default_sequence[2].step_type == "cycle"


class TestAppConfigIncludesCycle:
    """测试 AppConfig 包含 cycle 字段"""

    def test_appconfig_has_cycle_field(self):
        app_cfg = AppConfig()
        assert hasattr(app_cfg, "cycle")
        assert isinstance(app_cfg.cycle, CycleConfig)
