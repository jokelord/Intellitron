from __future__ import annotations

import copy
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _test_config(home: Path):
    from backend.config import AgentConfig, AppConfig, ModelConfig
    from backend.computer_use.config import ComputerUseConfig

    return AppConfig(
        model=ModelConfig(model_type="vision"),
        agent=AgentConfig(entry_agent_name="basic_agent", work_dir=str(home)),
        computer_use=ComputerUseConfig(enabled=True),
    )


def _tilde(path: Path, home: Path) -> str:
    return "~/" + path.relative_to(home).as_posix()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("use_tilde", [False, True])
def test_acceptance_benchmark_expands_artifact_dir(home, monkeypatch, use_tilde):
    from scripts import benchmark_computer_use as cli

    artifact_dir = home / "benchmark artifacts"
    argument = _tilde(artifact_dir, home) if use_tilde else str(artifact_dir)
    monkeypatch.setattr(
        sys,
        "argv",
        ["benchmark_computer_use", "--backend", "wayland", "--artifact-dir", argument, "--fixture-only"],
    )

    assert cli.main() == 2
    manifest = artifact_dir / "computer-use-acceptance.json"
    assert manifest.is_file()
    assert json.loads(manifest.read_text())["fixture_only"] is True


def test_calibration_cli_expands_fixture_and_sample_paths(home, monkeypatch, capsys):
    from scripts import calibrate_computer_use as cli

    app = _test_config(home)
    fingerprint = "offline-path-test"
    monkeypatch.setattr(cli, "get_config", lambda: app)
    monkeypatch.setattr(cli, "deployment_fingerprint", lambda _app: fingerprint)
    monkeypatch.setattr(cli, "verify_deployment_report", lambda *_args: None)
    generated = {}
    monkeypatch.setattr(
        cli,
        "generate_fixtures",
        lambda _config, _fingerprint, output_dir, **_kwargs: generated.setdefault(
            "path", Path(output_dir)
        ),
    )
    fixture_dir = home / "offline fixtures"
    monkeypatch.setattr(sys, "argv", ["calibrate", "--generate-fixtures", _tilde(fixture_dir, home)])
    assert cli.main() == 0
    assert generated["path"] == fixture_dir

    samples_path = home / "measured samples.json"
    output_path = home / "reports" / "measured.json"
    samples_path.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "desktop_geometry": None,
                "cases": [
                    {
                        "case_id": "offline-case",
                        "width": 100,
                        "height": 100,
                        "target_ratio": [0.5, 0.5],
                        "outputs": [[0.5, 0.5]],
                    }
                ],
            }
        )
    )
    output_path.parent.mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", [
        "calibrate", "--samples", _tilde(samples_path, home), "--output", _tilde(output_path, home),
        "--max-axis-error", "1", "--min-pass-rate", "1",
    ])
    assert cli.main() == 0
    assert json.loads(output_path.read_text())["fingerprint"] == fingerprint


def test_fixture_generator_accepts_tilde_output_dir(home, monkeypatch):
    from scripts.calibrate_computer_use import generate_fixtures

    output = home / "generated fixtures"
    app = _test_config(home)
    result = generate_fixtures(app.computer_use, "synthetic-only", _tilde(output, home),
                               target_size_px=3)

    assert result["cases"]
    assert (output / "samples.json").is_file()
    assert (output / "case-0-source.png").is_file()


def _write_tiny_safetensors(path: Path) -> None:
    header = {
        "model.language_model.visual.patch_embed.weight": {
            "dtype": "BF16",
            "shape": [1],
            "data_offsets": [0, 1],
        }
    }
    encoded = json.dumps(header, separators=(",", ":")).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(encoded)) + encoded + b"x")


def test_repair_callables_expand_tilde_paths_on_synthetic_weights(home):
    from scripts.repair_qwen36_vision_safetensors import (
        build_repaired_model_directory,
        rewrite_vision_keys,
    )

    source_dir = home / "synthetic model"
    source = source_dir / "model.safetensors"
    _write_tiny_safetensors(source)
    target = home / "rewritten" / "model.safetensors"
    assert rewrite_vision_keys(_tilde(source, home), _tilde(target, home)) == 1
    assert target.is_file()

    second_source_dir = home / "synthetic model copy"
    second_source = second_source_dir / "model.safetensors"
    _write_tiny_safetensors(second_source)
    (second_source_dir / "config.json").write_text("{}")
    output_dir = home / "repaired model"
    assert build_repaired_model_directory(_tilde(second_source, home), _tilde(output_dir, home)) == 1
    assert (output_dir / "model.safetensors").is_file()
    assert (output_dir / "config.json").read_text() == "{}"


def test_two_stage_probe_expands_fixture_and_saved_result_paths(home, monkeypatch):
    from scripts import probe_computer_use_two_stage as cli

    fixtures = home / "probe fixtures"
    fixtures.mkdir()
    app = _test_config(home)
    monkeypatch.setattr(cli, "get_config", lambda: app)
    monkeypatch.setattr(cli, "deployment_fingerprint", lambda _app: "probe-fingerprint")

    (fixtures / "samples.json").write_text(json.dumps({"fingerprint": "probe-fingerprint", "cases": []}))
    # The CLI takes argv from sys.argv; an empty manifest reaches the offline
    # no-cases guard only after it successfully reads the expanded fixture path.
    monkeypatch.setattr(sys, "argv", ["probe", "--fixtures", _tilde(fixtures, home)])
    with pytest.raises(SystemExit, match="no Stage-1 fixtures"):
        cli.main()
    assert (fixtures / "annotated" / "stage1").is_dir()

    (fixtures / "samples.json").write_text(json.dumps({
        "fingerprint": "probe-fingerprint",
        "cases": [{"case_id": "case-0-stage1", "width": 10, "height": 10}],
    }))
    missing_results = home / "missing stage1 results.json"
    monkeypatch.setattr(sys, "argv", [
        "probe", "--fixtures", _tilde(fixtures, home), "--stage1-results", _tilde(missing_results, home),
    ])
    with pytest.raises(FileNotFoundError) as error:
        cli.main()
    assert Path(error.value.filename) == missing_results


def _manifest_document(cases):
    from scripts.benchmark_read_file_v7 import MANIFEST_BEGIN, MANIFEST_END

    return f"{MANIFEST_BEGIN}{json.dumps({'schemaVersion': 1, 'cases': cases})}{MANIFEST_END}"


def test_read_file_manifest_and_smoke_cli_expand_tilde_paths(home, monkeypatch):
    from scripts import benchmark_read_file_v7 as cli

    manifest = home / "read-file acceptance.md"
    manifest.write_text(_manifest_document([{"id": "offline-smoke"}]))
    assert cli.load_manifest(_tilde(manifest, home))["cases"][0]["id"] == "offline-smoke"

    class QuietSampler:
        def start(self):
            pass

        def stop(self):
            pass

        def report(self):
            return None, None

    monkeypatch.setattr(cli, "_TegrastatsSampler", QuietSampler)
    output = home / "benchmark reports" / "smoke.json"
    assert cli.main([
        "--manifest", _tilde(manifest, home), "--output", _tilde(output, home), "--smoke",
    ]) == 0
    assert json.loads(output.read_text())["smoke"] is True
    assert output.with_suffix(".md").is_file()


def test_render_and_agent_fixture_helpers_expand_tilde_paths(home, monkeypatch):
    import base64

    from PIL import Image
    from scripts import collect_computer_use_same_agent as calibration
    from scripts.probe_computer_use_skill import _save_input
    from scripts.probe_computer_use_two_stage import contact_sheet

    skill = home / "offline skill.md"
    skill.write_text("offline fixture skill")
    monkeypatch.setattr(calibration, "_SKILL", skill)
    fixture_executor = calibration.FixtureDesktop.__new__(calibration.FixtureDesktop)
    assert fixture_executor.execute("read_file", {"path": _tilde(skill, home)}) == "offline fixture skill"

    image = Image.new("RGB", (8, 8), "white")
    sheet_path = home / "sheets" / "contact.png"
    contact_sheet([("offline", image)], _tilde(sheet_path, home))
    assert sheet_path.is_file()

    payload = b"synthetic png payload"
    encoded = SimpleNamespace(messages=[{"content": [
        {"text": "offline"},
        {"image_url": {"url": "data:image/png;base64," + base64.b64encode(payload).decode()}},
    ]}])
    image_path = home / "model inputs" / "stage1.png"
    image_path.parent.mkdir()
    _save_input(encoded, payload, _tilde(image_path, home))
    assert image_path.read_bytes() == payload


@pytest.mark.parametrize(
    "module_name, argv",
    [
        ("probe_computer_use_input", ["--authorize"]),
        ("probe_computer_use_live", ["--authorize"]),
        ("probe_computer_use_skill", ["--authorize", "--send-test-click"]),
    ],
)
def test_probe_cli_helper_paths_accept_tilde(home, monkeypatch, module_name, argv):
    import importlib

    module = importlib.import_module(f"scripts.{module_name}")
    helper = home / "native helper"
    helper.write_text("synthetic helper placeholder")
    expected = {}
    app = _test_config(home)
    monkeypatch.setattr(module, "get_config", lambda: copy.deepcopy(app))

    def fake_probe(*args, **kwargs):
        expected["helper"] = (
            kwargs.get("helper_binary") if module_name == "probe_computer_use_skill"
            else args[0].computer_use.helper_binary
        )
        if module_name == "probe_computer_use_skill":
            return {"input_outcome": "verified_in_test_target"}
        return {"input_outcome": "verified_in_test_window"}

    monkeypatch.setattr(module, "run_probe", fake_probe)
    full_argv = [*argv, "--helper-binary", _tilde(helper, home)]
    if module_name == "probe_computer_use_skill":
        full_argv.extend(["--api", "http://127.0.0.1:30000"])
    assert module.main(full_argv) == 0
    assert expected["helper"] == str(helper)


@pytest.mark.parametrize("module_name", ["probe_computer_use_input", "probe_computer_use_live"])
def test_probe_callable_expands_configured_helper_before_native_start(home, monkeypatch, module_name):
    import importlib

    module = importlib.import_module(f"scripts.{module_name}")
    captured = {}
    runtime = SimpleNamespace(
        command_mode="allowlist", allowed_executables=(), command_cwd_allowlist=(),
        command_blacklist=(), max_exec_timeout_seconds=1, max_output_bytes=1024,
        termination_grace_seconds=0.1,
    )

    class Capability:
        def command_runtime_config(self):
            return runtime

    class Supervisor:
        def __init__(self, **_kwargs):
            pass

        def shutdown(self):
            pass

    class Native:
        def __init__(self, *, helper_binary, **_kwargs):
            captured["helper"] = helper_binary

        def start(self, *_args, **_kwargs):
            return SimpleNamespace(ready=False, backend="wayland")

        def close(self):
            pass

    monkeypatch.setattr(module, "build_session_capability", lambda *_args, **_kwargs: Capability())
    monkeypatch.setattr(module, "require_capability", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "ProcessSupervisor", Supervisor)
    monkeypatch.setattr(module, "CommandPolicy", lambda **_kwargs: object())
    monkeypatch.setattr(module, "EnvironmentBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(module, "NativeClient", Native)
    app = SimpleNamespace(computer_use=SimpleNamespace(
        helper_binary="~/native helper", max_control_packet_bytes=1024,
        max_canonical_frame_bytes=2048, transport_timeout_seconds=1,
        authorization_timeout_seconds=1,
    ))

    if module_name == "probe_computer_use_input":
        module.run_probe(app, authorization_timeout=1, transport_timeout=1)
    else:
        with pytest.raises(module.ProbeFailure):
            module.run_probe(app, authorization_timeout=1, transport_timeout=1)
    assert captured["helper"] == str(home / "native helper")


def test_skill_probe_callable_accepts_tilde_helper_without_live_io(home, monkeypatch):
    from scripts import probe_computer_use_skill as cli

    monkeypatch.setattr(cli, "DIAGRAMS_DIR", home / "probe artifacts")
    monkeypatch.setattr(cli, "_require_production_disabled", lambda _app: None)
    monkeypatch.setattr(cli, "_expected_config", lambda _app: object())
    monkeypatch.setattr(
        cli, "model_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline stop")),
    )
    app = SimpleNamespace(computer_use=SimpleNamespace())

    report = cli.run_probe(
        app, helper_binary="~/native helper", api="http://127.0.0.1:30000",
        authorization_timeout=1, transport_timeout=1,
    )

    assert report["code"] == "RuntimeError"
    assert (home / "probe artifacts").is_dir()


def test_agent_cli_and_callable_accept_tilde_helper_and_safety_paths(home, monkeypatch, capsys):
    from backend.tools import skill_ops
    from scripts import run_computer_use_agent_test as cli

    helper = home / "bin" / "operator-helper"
    helper.parent.mkdir()
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o700)
    safety = home / "operator safety.yaml"
    safety.write_text("synthetic test preset")
    limits = {
        "max_observation_age_seconds": 10,
        "max_sample_age_seconds": 1,
        "max_validation_to_dispatch_seconds": 0.1,
        "global_diff_threshold": 0.01,
        "target_diff_threshold": 0.01,
        "double_click_interval_seconds": 0.1,
    }
    seen = {}
    def fake_load(path):
        seen["safety"] = Path(path)
        return dict(limits), {"transport_timeout_seconds": 15, "authorization_timeout_seconds": 120}

    monkeypatch.setattr(cli, "load_operator_preset", fake_load)
    monkeypatch.setattr(cli, "deployment_fingerprint", lambda _deployment: "synthetic-fingerprint")
    monkeypatch.setattr(skill_ops, "list_skills", lambda: ["computer-use-vision"])
    deployment = _test_config(home)
    monkeypatch.setattr(cli, "get_config", lambda: copy.deepcopy(deployment))

    # The public callable must also accept a home-relative helper path.
    app, _fingerprint = cli.prepare_operator_test_app(
        copy.deepcopy(deployment), helper_binary=_tilde(helper, home), safety_limits=limits,
        test_timeouts={"transport_timeout_seconds": 15, "authorization_timeout_seconds": 120},
    )
    assert app.computer_use.helper_binary == str(helper)

    assert cli.main([
        "--check-only", "--target-description", "offline test target",
        "--helper-binary", _tilde(helper, home), "--safety-config", _tilde(safety, home),
    ]) == 0
    assert seen["safety"] == safety
    assert json.loads(capsys.readouterr().out)["check_only"] is True


def test_same_agent_calibration_expands_paths_before_output_root_check(home, monkeypatch):
    from scripts import collect_computer_use_same_agent as cli

    fixtures = home / "offline fixture input"
    fixtures.mkdir()
    (fixtures / "samples.json").write_text(json.dumps({"fingerprint": "different", "cases": []}))
    allowed_root = home / "allowed evidence"
    monkeypatch.setattr(cli, "DIAGRAMS_ROOT", allowed_root)
    monkeypatch.setattr(cli, "deployment_fingerprint", lambda _app: "current")
    app = SimpleNamespace(
        model=SimpleNamespace(model_type="vision"),
        agent=SimpleNamespace(entry_agent_name="basic_agent", allow_desktop_capture=False,
                              allow_desktop_input=False),
        computer_use=SimpleNamespace(),
    )

    with pytest.raises(ValueError, match="fixture fingerprint differs"):
        cli.run_fixture_calibration(
            app, fixtures=_tilde(fixtures, home), output_dir=_tilde(allowed_root / "run", home),
        )
    with pytest.raises(ValueError, match="real-model evidence must remain"):
        cli.run_fixture_calibration(
            app, fixtures=_tilde(fixtures, home), output_dir=_tilde(home / "outside", home),
        )


def test_same_agent_cli_expands_both_path_arguments(home, monkeypatch, capsys):
    from scripts import collect_computer_use_same_agent as cli

    fixtures = home / "fixtures"
    output = home / "evidence run"
    captured = {}
    monkeypatch.setattr(cli, "get_config", lambda: object())

    def fake_run(_app, *, fixtures, output_dir, **_kwargs):
        captured["fixtures"] = Path(fixtures)
        captured["output_dir"] = Path(output_dir)
        return {"cases": []}

    monkeypatch.setattr(cli, "run_fixture_calibration", fake_run)
    assert cli.main([
        "--fixtures", _tilde(fixtures, home), "--output-dir", _tilde(output, home),
    ]) is None
    assert captured == {"fixtures": fixtures, "output_dir": output}
    assert json.loads(capsys.readouterr().out)["passed"] is False
