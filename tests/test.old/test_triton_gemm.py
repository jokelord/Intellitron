"""Test Triton GEMM kernel on SM110 to bypass cuBLAS."""
import torch
import triton
import triton.language as tl


@triton.jit
def _mm_kernel(
    A_ptr, B_ptr, C_ptr,
    M, N, K,
    stride_am, stride_ak,
    stride_bk, stride_bn,
    stride_cm, stride_cn,
    BLOCK_M: tl.constexpr,
    BLOCK_N: tl.constexpr,
    BLOCK_K: tl.constexpr,
):
    pid = tl.program_id(0)
    num_pid_n = tl.cdiv(N, BLOCK_N)
    pid_m = pid // num_pid_n
    pid_n = pid % num_pid_n

    offs_m = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    offs_n = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)

    acc = tl.zeros((BLOCK_M, BLOCK_N), dtype=tl.float32)

    for k_start in range(0, K, BLOCK_K):
        offs_k = k_start + tl.arange(0, BLOCK_K)
        a = tl.load(
            A_ptr + offs_m[:, None] * stride_am + offs_k[None, :] * stride_ak,
            mask=(offs_m[:, None] < M) & (offs_k[None, :] < K),
            other=0.0,
        )
        b = tl.load(
            B_ptr + offs_k[:, None] * stride_bk + offs_n[None, :] * stride_bn,
            mask=(offs_k[:, None] < K) & (offs_n[None, :] < N),
            other=0.0,
        )
        acc += tl.dot(a, b)

    c = acc.to(tl.bfloat16)
    mask_c = (offs_m[:, None] < M) & (offs_n[None, :] < N)
    tl.store(
        C_ptr + offs_m[:, None] * stride_cm + offs_n[None, :] * stride_cn,
        c,
        mask=mask_c,
    )


def triton_mm(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
    """a @ b using Triton (cuBLAS-free). a=[M,K], b=[K,N] -> [M,N]"""
    M, K = a.shape
    K2, N = b.shape
    assert K == K2
    c = torch.empty((M, N), device=a.device, dtype=a.dtype)
    BLOCK_M, BLOCK_N, BLOCK_K = 64, 64, 64
    grid = (triton.cdiv(M, BLOCK_M) * triton.cdiv(N, BLOCK_N),)
    _mm_kernel[grid](
        a, b, c,
        M, N, K,
        a.stride(0), a.stride(1),
        b.stride(0), b.stride(1),
        c.stride(0), c.stride(1),
        BLOCK_M=BLOCK_M, BLOCK_N=BLOCK_N, BLOCK_K=BLOCK_K,
    )
    return c


if __name__ == "__main__":
    # Test 1: small size
    print("Test 1: [256, 1152] x [1152, 3456]")
    a = torch.randn(256, 1152, device="cuda", dtype=torch.bfloat16)
    w = torch.randn(3456, 1152, device="cuda", dtype=torch.bfloat16)
    b = w.t().contiguous()
    c = triton_mm(a, b)
    torch.cuda.synchronize()
    ref = a.float() @ b.float()
    err = (c.float() - ref).abs().max().item()
    print(f"  PASSED, max error = {err:.6f}")

    # Test 2: actual vision encoder size (M=65536)
    print("Test 2: [65536, 1152] x [1152, 3456]")
    a2 = torch.randn(65536, 1152, device="cuda", dtype=torch.bfloat16)
    b2 = w.t().contiguous()
    c2 = triton_mm(a2, b2)
    torch.cuda.synchronize()
    ref2 = a2.float() @ b2.float()
    err2 = (c2.float() - ref2).abs().max().item()
    print(f"  PASSED, max error = {err2:.6f}")

    # Test 3: MLP size (up projection)
    print("Test 3: [65536, 1152] x [1152, 4608]")
    w3 = torch.randn(4608, 1152, device="cuda", dtype=torch.bfloat16)
    b3 = w3.t().contiguous()
    c3 = triton_mm(a2, b3)
    torch.cuda.synchronize()
    ref3 = a2.float() @ b3.float()
    err3 = (c3.float() - ref3).abs().max().item()
    print(f"  PASSED, max error = {err3:.6f}")

    print("\nAll Triton GEMM tests passed!")
