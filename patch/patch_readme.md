# Triton PTXAS Wrapper (核心补丁)
Triton 在编译 kernel 时会向 `ptxas` 传递 `--gpu-name=sm_110a`，但现有的编译工具仅支持 `sm_110`。
**解决方案**：创建一个 `/home/zk/ptxas_wrapper.sh` 并在 `.bashrc` 中指定 `export TRITON_PTXAS_PATH=/home/zk/ptxas_wrapper.sh`。