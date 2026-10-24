"""Preflight the prepared SGLang patch without modifying installed dependencies."""

import ast
import importlib.util
import os
from pathlib import Path
import subprocess
from typing import Optional, Union

import pytest
import torch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PATCH = PROJECT_ROOT / "patch/sglang-flashinfer-fast-prefill-uniform-q-len.patch"
RELATIVE_SOURCE = Path("sglang/srt/layers/attention/flashinfer_backend.py")


def _installed_source():
    return Path(importlib.util.find_spec("sglang").origin).parent.parent / RELATIVE_SOURCE


def _extract_fast_plan(source):
    function = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "fast_prefill_plan"
    )
    namespace = {"torch": torch, "Optional": Optional, "Union": Union, "os": os}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<installed-fast-plan>", "exec"), namespace)
    return namespace["fast_prefill_plan"]


def _patched_source(tmp_path, installed=None):
    installed = installed or _installed_source()
    original_bytes = installed.read_bytes()
    staged = tmp_path / RELATIVE_SOURCE
    staged.parent.mkdir(parents=True)
    staged.write_bytes(original_bytes)
    command = ["patch", "--batch", "--fuzz=0", "-p1", "-d", str(tmp_path), "-i", str(PATCH)]
    def run(extra=()):
        return subprocess.run(command + list(extra), capture_output=True, text=True, timeout=10)

    if run(("--dry-run", "--forward")).returncode != 0:
        # After installation, recover the original solely in this temporary copy.
        reverse = run(("--reverse",))
        assert reverse.returncode == 0, reverse.stdout + reverse.stderr
    baseline = staged.read_text()
    result = run(("--forward",))
    assert result.returncode == 0, result.stdout + result.stderr
    assert installed.read_bytes() == original_bytes
    return baseline, staged.read_text()


def test_patch_applies_cleanly_without_changing_dependency(tmp_path):
    _patched_source(tmp_path)


def test_preflight_also_accepts_already_patched_source_without_changing_it(tmp_path):
    original, patched = _patched_source(tmp_path / "first")
    already_patched = tmp_path / "already_patched.py"
    already_patched.write_text(patched)
    baseline, candidate = _patched_source(tmp_path / "second", installed=already_patched)
    assert baseline == original
    assert candidate == patched
    assert already_patched.read_text() == patched


def test_patched_fast_plan_matches_real_cuda_fa2_planner(tmp_path, monkeypatch):
    if not torch.cuda.is_available():
        pytest.skip("Real CUDA planner preflight requires CUDA")
    from flashinfer import BatchPrefillWithPagedKVCacheWrapper
    from flashinfer_jit_cache import get_jit_cache_dir

    uri = (
        "batch_prefill_with_kv_cache_dtype_q_bf16_dtype_kv_e4m3_dtype_o_bf16_"
        "dtype_idx_i32_head_dim_qk_256_head_dim_vo_256_posenc_0_use_swa_False_"
        "use_logits_cap_False_f16qk_False"
    )
    if not (Path(get_jit_cache_dir()) / uri / (uri + ".so")).exists():
        pytest.skip("Matching AOT module absent; avoid an expensive JIT compile")
    original, patched = _patched_source(tmp_path)
    qo_host = torch.tensor([0, 1], dtype=torch.int32)
    kv_host = torch.tensor([0, 1], dtype=torch.int32)
    kv_lens = torch.tensor([1], dtype=torch.int32)
    qo, kv = qo_host.cuda(), kv_host.cuda()
    indices = torch.tensor([0], dtype=torch.int32, device="cuda")
    last_page = torch.tensor([1], dtype=torch.int32, device="cuda")
    wrapper = BatchPrefillWithPagedKVCacheWrapper(
        torch.empty(16 * 1024 * 1024, dtype=torch.uint8, device="cuda"),
        use_cuda_graph=True, backend="fa2",
        qo_indptr_buf=torch.empty_like(qo),
        paged_kv_indptr_buf=torch.empty_like(kv),
        paged_kv_indices_buf=torch.empty_like(indices),
        paged_kv_last_page_len_buf=torch.empty_like(last_page),
    )
    args = (qo, kv, indices, last_page, 24, 4, 256, 1)
    kwargs = dict(q_data_type=torch.bfloat16, kv_data_type=torch.float8_e4m3fn)
    wrapper.plan(*args, **kwargs)
    upstream_plan = tuple(wrapper._plan_info)
    fast_kwargs = dict(
        **kwargs, qo_indptr_host=qo_host, kv_indptr_host=kv_host,
        kv_lens_host=kv_lens, max_q_len=1, max_kv_len=1,
    )
    with pytest.raises(Exception, match="20.*19|19.*20"):
        _extract_fast_plan(original)(wrapper, *args, **fast_kwargs)
    monkeypatch.delenv("LLM_DEPLOYMENT_FLASHINFER_PREFILL_UNIFORM_Q_LEN_COMPAT", raising=False)
    with pytest.raises(Exception, match="20.*19|19.*20"):
        _extract_fast_plan(patched)(wrapper, *args, **fast_kwargs)
    monkeypatch.setenv("LLM_DEPLOYMENT_FLASHINFER_PREFILL_UNIFORM_Q_LEN_COMPAT", "1")
    _extract_fast_plan(patched)(wrapper, *args, **fast_kwargs)
    torch.cuda.synchronize()
    assert tuple(wrapper._plan_info) == upstream_plan
