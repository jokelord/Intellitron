#!/usr/bin/env python3
"""
Diagnose which matrix dimensions trigger CUBLAS_STATUS_INVALID_VALUE on SM110.
Tests the exact shapes used by Qwen3-VL vision encoder QKV projection.
"""
import torch
import sys

device = "cuda"

# Vision encoder dimensions from Qwen3-VL config
# hidden_size = 1152, num_heads = 16, head_dim = 72
# QKV output = 3 * hidden_size = 3456
# max_image_tokens = 16384

test_cases = [
    # (name, M, K, N) for matmul: [M, K] x [K, N] = [M, N]
    ("warmup_8x8",            8,     8,     8),
    ("small_square_64",       64,    64,    64),
    ("small_square_256",      256,   256,   256),
    ("medium_1024x1152",      1024,  1152,  3456),
    ("qkv_4096x1152",         4096,  1152,  3456),
    ("qkv_8192x1152",         8192,  1152,  3456),
    ("qkv_16384x1152",        16384, 1152,  3456),
    # MLP dimensions (hidden_size -> 4*hidden_size typical)
    ("mlp_up_16384x1152",     16384, 1152,  4608),
    ("mlp_down_16384x4608",   16384, 4608,  1152),
    # Check if it's a specific dimension alignment issue
    ("qkv_16384x1152_padded", 16384, 1152,  3456),
    ("qkv_16000x1152",        16000, 1152,  3456),
    ("qkv_16384x1024",        16384, 1024,  3072),
    ("qkv_16384x1280",        16384, 1280,  3840),
    # 3D batched (simulating [seq, batch, hidden])
    ("qkv_3d_1x16384x1152",  None,  None,  None),  # special case
]


def test_matmul(name, m, k, n, dtype):
    """Test a single matmul configuration."""
    dtype_name = str(dtype).split(".")[-1]
    try:
        x = torch.randn(m, k, device=device, dtype=dtype)
        w = torch.randn(n, k, device=device, dtype=dtype)
        out = x.matmul(w.t())
        torch.cuda.synchronize()
        del x, w, out
        print(f"  OK   {dtype_name:8s} [{m:>6d}, {k:>5d}] x [{k:>5d}, {n:>5d}]  {name}")
        return True
    except RuntimeError as e:
        print(f"  FAIL {dtype_name:8s} [{m:>6d}, {k:>5d}] x [{k:>5d}, {n:>5d}]  {name}  -- {e}")
        # Reset CUDA error state
        torch.cuda.synchronize()
        return False


def test_3d_matmul(dtype):
    """Test 3D batched matmul [seq, batch, hidden] x [hidden, out]."""
    dtype_name = str(dtype).split(".")[-1]
    try:
        x = torch.randn(16384, 1, 1152, device=device, dtype=dtype)
        w = torch.randn(3456, 1152, device=device, dtype=dtype)
        out = x.matmul(w.t())
        torch.cuda.synchronize()
        del x, w, out
        print(f"  OK   {dtype_name:8s} [16384, 1, 1152] x [1152, 3456]  3D batched")
        return True
    except RuntimeError as e:
        print(f"  FAIL {dtype_name:8s} [16384, 1, 1152] x [1152, 3456]  3D batched  -- {e}")
        torch.cuda.synchronize()
        return False


def test_flinear(name, m, k, n, dtype):
    """Test F.linear (uses different cuBLAS codepath when bias is present)."""
    import torch.nn.functional as F
    dtype_name = str(dtype).split(".")[-1]
    try:
        x = torch.randn(m, k, device=device, dtype=dtype)
        w = torch.randn(n, k, device=device, dtype=dtype)
        b = torch.randn(n, device=device, dtype=dtype)
        out = F.linear(x, w, b)
        torch.cuda.synchronize()
        del x, w, b, out
        print(f"  OK   {dtype_name:8s} F.linear [{m:>6d}, {k:>5d}] w=[{n:>5d}, {k:>5d}]  {name}")
        return True
    except RuntimeError as e:
        print(f"  FAIL {dtype_name:8s} F.linear [{m:>6d}, {k:>5d}] w=[{n:>5d}, {k:>5d}]  {name}  -- {e}")
        torch.cuda.synchronize()
        return False


def test_chunked_matmul(m, k, n, chunk_size, dtype):
    """Test if chunking the matmul works around the issue."""
    dtype_name = str(dtype).split(".")[-1]
    try:
        x = torch.randn(m, k, device=device, dtype=dtype)
        w = torch.randn(n, k, device=device, dtype=dtype)
        chunks = x.split(chunk_size, dim=0)
        results = [chunk.matmul(w.t()) for chunk in chunks]
        out = torch.cat(results, dim=0)
        torch.cuda.synchronize()
        del x, w, chunks, results, out
        print(f"  OK   {dtype_name:8s} chunked({chunk_size}) [{m}, {k}] x [{k}, {n}]")
        return True
    except RuntimeError as e:
        print(f"  FAIL {dtype_name:8s} chunked({chunk_size}) [{m}, {k}] x [{k}, {n}]  -- {e}")
        torch.cuda.synchronize()
        return False


def test_torch_mm(dtype):
    """Test torch.mm specifically (always 2D, no broadcasting)."""
    dtype_name = str(dtype).split(".")[-1]
    try:
        x = torch.randn(16384, 1152, device=device, dtype=dtype)
        w = torch.randn(1152, 3456, device=device, dtype=dtype)
        out = torch.mm(x, w)
        torch.cuda.synchronize()
        del x, w, out
        print(f"  OK   {dtype_name:8s} torch.mm [16384, 1152] x [1152, 3456]")
        return True
    except RuntimeError as e:
        print(f"  FAIL {dtype_name:8s} torch.mm [16384, 1152] x [1152, 3456]  -- {e}")
        torch.cuda.synchronize()
        return False


def test_einsum(dtype):
    """Test torch.einsum as alternative backend."""
    dtype_name = str(dtype).split(".")[-1]
    try:
        x = torch.randn(16384, 1152, device=device, dtype=dtype)
        w = torch.randn(3456, 1152, device=device, dtype=dtype)
        out = torch.einsum("mk,nk->mn", x, w)
        torch.cuda.synchronize()
        del x, w, out
        print(f"  OK   {dtype_name:8s} einsum mk,nk->mn [16384, 1152] x [3456, 1152]")
        return True
    except RuntimeError as e:
        print(f"  FAIL {dtype_name:8s} einsum  -- {e}")
        torch.cuda.synchronize()
        return False


def test_addmm(dtype):
    """Test torch.addmm (may use different cuBLAS path)."""
    dtype_name = str(dtype).split(".")[-1]
    try:
        x = torch.randn(16384, 1152, device=device, dtype=dtype)
        w = torch.randn(1152, 3456, device=device, dtype=dtype)
        bias = torch.randn(3456, device=device, dtype=dtype)
        out = torch.addmm(bias, x, w)
        torch.cuda.synchronize()
        del x, w, bias, out
        print(f"  OK   {dtype_name:8s} torch.addmm [16384, 1152] x [1152, 3456]")
        return True
    except RuntimeError as e:
        print(f"  FAIL {dtype_name:8s} torch.addmm  -- {e}")
        torch.cuda.synchronize()
        return False


if __name__ == "__main__":
    print(f"PyTorch: {torch.__version__}")
    print(f"CUDA: {torch.version.cuda}")
    print(f"Device: {torch.cuda.get_device_name()}")
    print(f"Capability: {torch.cuda.get_device_capability()}")
    print()

    # Check preferred BLAS library
    try:
        lib = torch.backends.cuda.preferred_blas_library()
        print(f"Preferred BLAS library: {lib}")
    except Exception:
        print("Preferred BLAS library: (not available)")
    print()

    dtypes = [torch.float32, torch.float16, torch.bfloat16]

    print("=" * 80)
    print("1. Standard matmul tests (x.matmul(w.t()))")
    print("=" * 80)
    for dtype in dtypes:
        for name, m, k, n in test_cases:
            if m is None:
                continue
            test_matmul(name, m, k, n, dtype)
        print()

    print("=" * 80)
    print("2. 3D batched matmul tests")
    print("=" * 80)
    for dtype in dtypes:
        test_3d_matmul(dtype)
    print()

    print("=" * 80)
    print("3. torch.mm tests (strict 2D)")
    print("=" * 80)
    for dtype in dtypes:
        test_torch_mm(dtype)
    print()

    print("=" * 80)
    print("4. torch.einsum tests")
    print("=" * 80)
    for dtype in dtypes:
        test_einsum(dtype)
    print()

    print("=" * 80)
    print("5. torch.addmm tests")
    print("=" * 80)
    for dtype in dtypes:
        test_addmm(dtype)
    print()

    print("=" * 80)
    print("6. F.linear with bias tests (the original failing path)")
    print("=" * 80)
    for dtype in dtypes:
        test_flinear("qkv_16384", 16384, 1152, 3456, dtype)
    print()

    print("=" * 80)
    print("7. Chunked matmul (workaround test)")
    print("=" * 80)
    for dtype in dtypes:
        for chunk in [1024, 2048, 4096, 8192]:
            test_chunked_matmul(16384, 1152, 3456, chunk, dtype)
        print()

    print("=" * 80)
    print("8. Binary search for max working M dimension")
    print("=" * 80)
    for dtype in dtypes:
        dtype_name = str(dtype).split(".")[-1]
        lo, hi = 1, 16384
        while lo < hi:
            mid = (lo + hi + 1) // 2
            ok = test_matmul(f"bsearch_m={mid}", mid, 1152, 3456, dtype)
            if ok:
                lo = mid
            else:
                hi = mid - 1
        print(f"  >> {dtype_name}: max working M = {lo}")
        print()

    torch.cuda.empty_cache()
    print("Done.")
