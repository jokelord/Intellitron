"""Verify checkpoint prefix compatibility through the installed weight loaders."""

from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import sys

import pytest
import torch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("prefix", ["model.language_model.", "model."])
def test_text_adapter_loads_checkpoint_and_regular_prefixes(prefix):
    from sglang.srt.models.qwen3_5 import Qwen3_5ForCausalLM as BodyModel
    from sglang_extensions.qwen3_5_text import Qwen3_5ForCausalLM

    body = BodyModel.__new__(BodyModel)
    torch.nn.Module.__init__(body)
    body.embed_tokens = torch.nn.Embedding(2, 2)
    body.embed_tokens.weight.data.zero_()
    model = Qwen3_5ForCausalLM.__new__(Qwen3_5ForCausalLM)
    torch.nn.Module.__init__(model)
    model.model = body
    model.config = SimpleNamespace(tie_word_embeddings=False)
    model.pp_group = SimpleNamespace(is_last_rank=True)
    model.lm_head = torch.nn.Linear(2, 2, bias=False)
    model.lm_head.weight.data.zero_()
    embedding = torch.tensor([[1., 2.], [3., 4.]])
    head = torch.tensor([[5., 6.], [7., 8.]])
    weights = [(prefix + "embed_tokens.weight", embedding), ("lm_head.weight", head)]

    loaded = model.load_weights(iter(weights))

    assert loaded == {"model.embed_tokens.weight", "lm_head.weight"}
    torch.testing.assert_close(body.embed_tokens.weight, embedding)
    torch.testing.assert_close(model.lm_head.weight, head)
    assert weights[0][0] == prefix + "embed_tokens.weight"
    assert weights[0][1] is embedding
    assert weights[1][1] is head


def test_opted_in_registry_selects_adapter_and_keeps_installed_mtp():
    env = dict(os.environ)
    env["SGLANG_EXTERNAL_MODEL_PACKAGE"] = "sglang_extensions"
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", """
from sglang.srt.models.registry import ModelRegistry
target, _ = ModelRegistry.resolve_model_cls(['Qwen3_5ForCausalLM'])
draft, _ = ModelRegistry.resolve_model_cls(['Qwen3_5ForCausalLMMTP'])
assert target.__module__ == 'sglang_extensions.qwen3_5_text', target
assert draft.__module__.startswith('sglang.srt.models.'), draft
"""],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
