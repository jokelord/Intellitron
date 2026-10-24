"""Configured startup rejection must reach the public desktop command boundary."""
import copy
from dataclasses import replace

import pytest
import yaml


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode,expected_code", [
    ("operator", "INVALID_OPERATOR_SESSION_CONFIG"),
    ("unexpected", "COMPUTER_USE_INITIALIZATION_FAILED"),
    ("disabled", "UNAVAILABLE"),
])
async def test_initialization_status_survives_startup_to_command_and_config(
        tmp_path, monkeypatch, caplog, failure_mode, expected_code):
    import backend.main as main
    from fastapi.testclient import TestClient
    from backend.computer_use.executable_protocol import execute_vision_desktop_command
    from backend.computer_use.operator_policy import OPERATOR_CONFIG_PATH
    from test_executable_backend import _capability
    config = copy.deepcopy(main.config)
    values = yaml.safe_load(OPERATOR_CONFIG_PATH.read_text())
    values['max_observation_age_seconds'] = 360
    preset = tmp_path / 'invalid-operator.yaml'
    preset.write_text(yaml.safe_dump(values))
    helper = tmp_path / 'helper'
    helper.write_text('no launch permitted')
    config.computer_use = replace(config.computer_use, enabled=True,
        operator_session_enabled=True, helper_binary=str(helper),
        operator_session_safety_config_path=str(preset), calibration_report_path=None,
        calibration_fingerprint=None)
    if failure_mode == 'disabled':
        config.computer_use = replace(config.computer_use, enabled=False)
    elif failure_mode == 'unexpected':
        def fail_initialization(_config):
            raise RuntimeError('private startup details')
        monkeypatch.setattr('backend.computer_use.runtime.build_runtime_backend', fail_initialization)
    config.agent.enabled = False
    config.agent.entry_agent_name = 'basic_agent'
    config.agent.allow_desktop_capture = config.agent.allow_desktop_input = True
    config.model.model_type = 'vision'
    config.cycle.enabled = config.compact.enabled = False
    config.asr.enabled = False
    monkeypatch.setattr(main, 'config', config)
    monkeypatch.setattr(main, 'reload_config', lambda: config)
    monkeypatch.setattr(main, '_validate_ocr_worker_count', lambda *_: None)
    monkeypatch.setattr('backend.llm_service.LLMService.initialize', lambda _: False)
    monkeypatch.setattr('backend.compact.tokenizer.TokenCounter.get', lambda **_: None)
    monkeypatch.setattr(main.app.state, 'computer_use_backend', None, raising=False)
    capability = _capability(tmp_path)
    async with main.lifespan(main.app):
        result = execute_vision_desktop_command('vision_desktop observe',
            session_id=capability.session_id, capability=capability)
        assert result.state == expected_code
        assert ('NATIVE_PLATFORM_INTEGRATION_UNIMPLEMENTED' in result.text) == (failure_mode == 'disabled')
        assert 'private startup details' not in result.text
        assert result.attachment is None
        status = TestClient(main.app).get('/config').json()['computer_use']
        assert status['runtime_mode'] == 'unavailable'
        assert status['initialization_error_code'] == (None if failure_mode == 'disabled' else expected_code)
        if failure_mode == 'operator':
            assert 'max_observation_age_seconds exceeds' in caplog.text
        # Calls remain pinned to the failed startup snapshot, not live YAML.
        preset.write_text(yaml.safe_dump({**values, 'max_observation_age_seconds': 180}))
        assert execute_vision_desktop_command('vision_desktop observe',
            session_id=capability.session_id, capability=capability).state == result.state
