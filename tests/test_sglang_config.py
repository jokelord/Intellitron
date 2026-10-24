"""Tests for SGLang config parsing."""

from pathlib import Path

import yaml

from backend.config import ConfigLoader, SGLangConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestParseSGLangConfig:
    def _make_loader_with_data(self, sglang_data: dict) -> ConfigLoader:
        loader = ConfigLoader.__new__(ConfigLoader)
        loader._data = {"sglang": sglang_data}
        loader._agent_prompts_data = {}
        return loader

    def test_parse_new_sglang_runtime_tuning_fields(self):
        loader = self._make_loader_with_data(
            {
                "cuda_graph_max_bs": 4,
                "kv_cache_dtype": "fp8_e4m3",
                "schedule_policy": "lpm",
                "chunked_prefill_size": 4096,
                "enable_cache_report": True,
            }
        )

        cfg = loader._parse_sglang_config()

        assert isinstance(cfg, SGLangConfig)
        assert cfg.cuda_graph_max_bs == 4
        assert cfg.kv_cache_dtype == "fp8_e4m3"
        assert cfg.schedule_policy == "lpm"
        assert cfg.chunked_prefill_size == 4096
        assert cfg.enable_cache_report is True

    def test_parse_new_sglang_runtime_tuning_fields_defaults_to_none(self):
        loader = self._make_loader_with_data({})

        cfg = loader._parse_sglang_config()

        assert cfg.cuda_graph_max_bs is None
        assert cfg.kv_cache_dtype is None
        assert cfg.schedule_policy is None
        assert cfg.enable_cache_report is None

    def test_parse_eagle_replayssm_fields(self):
        loader = self._make_loader_with_data(
            {
                "speculative_num_steps": 3,
                "speculative_eagle_topk": 1,
                "speculative_num_draft_tokens": 4,
                "enable_linear_replayssm_spec": True,
            }
        )

        cfg = loader._parse_sglang_config()

        assert cfg.speculative_num_steps == 3
        assert cfg.speculative_eagle_topk == 1
        assert cfg.speculative_num_draft_tokens == 4
        assert cfg.enable_linear_replayssm_spec is True

    def test_parse_eagle_replayssm_fields_defaults_to_none(self):
        cfg = self._make_loader_with_data({})._parse_sglang_config()

        assert cfg.speculative_num_steps is None
        assert cfg.speculative_eagle_topk is None
        assert cfg.speculative_num_draft_tokens is None
        assert cfg.enable_linear_replayssm_spec is None

    def test_parse_external_model_package(self):
        cfg = self._make_loader_with_data(
            {"external_model_package": "sglang_extensions"}
        )._parse_sglang_config()
        assert cfg.external_model_package == "sglang_extensions"

    def test_external_model_package_defaults_to_none(self):
        assert self._make_loader_with_data({})._parse_sglang_config().external_model_package is None

    def test_parse_flashinfer_uniform_q_len_compat(self):
        enabled = self._make_loader_with_data(
            {"flashinfer_prefill_uniform_q_len_compat": True}
        )._parse_sglang_config()
        disabled = self._make_loader_with_data(
            {"flashinfer_prefill_uniform_q_len_compat": False}
        )._parse_sglang_config()
        default = self._make_loader_with_data({})._parse_sglang_config()
        assert enabled.flashinfer_prefill_uniform_q_len_compat is True
        assert disabled.flashinfer_prefill_uniform_q_len_compat is False
        assert default.flashinfer_prefill_uniform_q_len_compat is None


def test_qwen3_5_thor_config_uses_flashinfer_attention_backend():
    """FlashInfer starts on SGLang 0.5.14 and avoids slow Triton attention."""

    config_path = PROJECT_ROOT / "config" / "qwen3__5-config-infer.yaml"
    data = yaml.safe_load(config_path.read_text())

    assert data["sglang"]["attention_backend"] == "flashinfer"


def test_qwen3_6_heretic_nvfp4_thor_config_uses_trtllm_fp4_moe_backend():
    """Thor SM110 must avoid SGLang's CUTLASS FP4 MoE path."""

    config_path = PROJECT_ROOT / "config" / "qwen3__6-herotic-NVFP4-config-infer.yaml"
    data = yaml.safe_load(config_path.read_text())

    assert data["sglang"]["moe_runner_backend"] == "flashinfer_trtllm"
