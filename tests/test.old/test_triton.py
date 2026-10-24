import torch
import os
os.environ["TRITON_CACHE_DIR"] = "/tmp/triton"
os.environ["TORCHINDUCTOR_ON_NVIDIA_TRITON"] = "1"
@torch.compile
def f(x):
    return x * 2.0 + 1.0

try:
    print(f(torch.tensor([1.0, 2.0]).cuda()))
except Exception as e:
    print("Error:", e)
