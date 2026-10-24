from __future__ import annotations

import pytest

from backend import config as config_module
from backend.computer_use.config import ComputerUseConfig


PATH_FIELDS = (
    "helper_binary",
    "calibration_report_path",
    "operator_session_safety_config_path",
)


def test_yaml_native_paths_follow_clone_root_not_process_cwd(tmp_path, monkeypatch):
    clone_root = tmp_path / "another-clone"
    unrelated_cwd = tmp_path / "working-directory"
    unrelated_cwd.mkdir()
    monkeypatch.setattr(config_module, "PROJECT_ROOT", clone_root)
    monkeypatch.chdir(unrelated_cwd)
    raw = {
        "helper_binary": "bin/linux-aarch64/computer-use-helper",
        "calibration_report_path": "logs/computer-use/calibration.json",
        "operator_session_enabled": True,
        "operator_session_safety_config_path": "config/computer-use-operator-session.yaml",
    }
    loader = config_module.ConfigLoader()
    loader._data = {"computer_use": raw}

    parsed = loader._parse_computer_use_config()

    assert parsed.helper_binary == str(clone_root / "bin/linux-aarch64/computer-use-helper")
    assert parsed.calibration_report_path == str(clone_root / "logs/computer-use/calibration.json")
    assert parsed.operator_session_safety_config_path == str(clone_root / "config/computer-use-operator-session.yaml")
    assert raw["helper_binary"] == "bin/linux-aarch64/computer-use-helper"


@pytest.mark.parametrize("field", PATH_FIELDS)
def test_yaml_absolute_native_path_override_is_preserved(field):
    loader = config_module.ConfigLoader()
    loader._data = {"computer_use": {field: "/opt/custom/../native-file"}}
    assert getattr(loader._parse_computer_use_config(), field) == "/opt/custom/../native-file"


@pytest.mark.parametrize("field", PATH_FIELDS)
def test_yaml_null_native_path_is_preserved(field):
    loader = config_module.ConfigLoader()
    loader._data = {"computer_use": {field: None}}
    assert getattr(loader._parse_computer_use_config(), field) is None


@pytest.mark.parametrize("field", PATH_FIELDS)
@pytest.mark.parametrize("bad", ["", "   ", "bad\x00path", True, 0, [], {}])
def test_yaml_native_path_normalization_keeps_invalid_values_rejected(field, bad):
    loader = config_module.ConfigLoader()
    loader._data = {"computer_use": {field: bad}}
    with pytest.raises(ValueError, match=field):
        loader._parse_computer_use_config()


@pytest.mark.parametrize("bad", ["not-a-mapping", [], True])
def test_yaml_native_path_normalization_keeps_invalid_subtree_rejected(bad):
    loader = config_module.ConfigLoader()
    loader._data = {"computer_use": bad}
    with pytest.raises(TypeError, match="mapping"):
        loader._parse_computer_use_config()


@pytest.mark.parametrize("field", PATH_FIELDS)
def test_runtime_config_still_requires_absolute_native_paths(field):
    with pytest.raises(ValueError, match="absolute path"):
        ComputerUseConfig.from_mapping({field: "relative/path"})
