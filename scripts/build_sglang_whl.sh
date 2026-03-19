#!/usr/bin/env bash
# =============================================================================
# build_sglang_whl.sh
#
# 从 sglang-main/ 源码编译 sgl-kernel 和 sglang 的 .whl 文件，
# 最终将 .whl 存放到 ./wheels/ 目录。
#
# 用法:
#   ./scripts/build_sglang_whl.sh [--cuda-home <path>] [--jobs <N>] [--skip-kernel] [--skip-sglang]
#
# 选项:
#   --cuda-home   指定 CUDA 安装路径（默认 /usr/local/cuda）
#   --jobs        并行编译线程数（默认 CPU 核心数）
#   --skip-kernel 跳过 sgl-kernel 构建
#   --skip-sglang 跳过 sglang 主包构建
# =============================================================================
set -euo pipefail

# ─── 路径配置 ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$REPO_ROOT/sglang-main"
WHEELS_DIR="$REPO_ROOT/wheels"
LOGS_DIR="$REPO_ROOT/logs"
DEPS_DIR="${DEPS_DIR:-$REPO_ROOT/sglang-deps}"   # FetchContent 依赖持久目录
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BUILD_LOG="$LOGS_DIR/build_sglang_${TIMESTAMP}.log"
KERNEL_BUILD_DIR="$SRC_DIR/sgl-kernel/build_whl"

# ─── 默认参数 ────────────────────────────────────────────────────────────────
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-13.0}"   # 固定使用 CUDA 13.0
JOBS="${MAX_JOBS:-6}"   # 降低并行数防止 OOM（可通过 --jobs 或 MAX_JOBS 环境变量覆盖）
SKIP_KERNEL=0
SKIP_SGLANG=0

# ─── 颜色输出 ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

log()  { local msg="[$(date '+%H:%M:%S')] $*"; echo -e "${CYAN}${msg}${RESET}" | tee -a "$BUILD_LOG"; }
ok()   { local msg="[$(date '+%H:%M:%S')] ✓ $*"; echo -e "${GREEN}${msg}${RESET}" | tee -a "$BUILD_LOG"; }
warn() { local msg="[$(date '+%H:%M:%S')] ⚠ $*"; echo -e "${YELLOW}${msg}${RESET}" | tee -a "$BUILD_LOG"; }
err()  { local msg="[$(date '+%H:%M:%S')] ✗ $*"; echo -e "${RED}${msg}${RESET}" | tee -a "$BUILD_LOG"; }
sep()  { echo -e "${BOLD}$(printf '─%.0s' {1..72})${RESET}" | tee -a "$BUILD_LOG"; }

# ─── 参数解析 ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cuda-home)   CUDA_HOME="$2";  shift 2 ;;
        --jobs)        JOBS="$2";       shift 2 ;;
        --skip-kernel) SKIP_KERNEL=1;   shift   ;;
        --skip-sglang) SKIP_SGLANG=1;   shift   ;;
        -h|--help)
            sed -n '3,15p' "$0" | sed 's/^# //'
            exit 0 ;;
        *) err "未知参数: $1"; exit 1 ;;
    esac
done

# ─── 准备目录 ────────────────────────────────────────────────────────────────
mkdir -p "$WHEELS_DIR" "$LOGS_DIR"

sep
echo -e "${BOLD}  SGLang 源码编译脚本${RESET}" | tee -a "$BUILD_LOG"
sep
log "仓库根目录  : $REPO_ROOT"
log "源码目录    : $SRC_DIR"
log "输出目录    : $WHEELS_DIR"
log "CUDA_HOME   : $CUDA_HOME"
log "并行线程    : $JOBS"
log "构建日志    : $BUILD_LOG"
sep

# ─── 环境检查 ────────────────────────────────────────────────────────────────
log "检查构建环境..."

# 检查源码目录
if [[ ! -d "$SRC_DIR/sgl-kernel" ]]; then
    err "源码目录不存在: $SRC_DIR/sgl-kernel"
    err "请确认 sglang-main/ 子目录结构完整"
    exit 1
fi

# 检查 CUDA
NVCC="$CUDA_HOME/bin/nvcc"
if [[ ! -x "$NVCC" ]]; then
    # 尝试系统 PATH
    NVCC="$(command -v nvcc 2>/dev/null || true)"
    if [[ -z "$NVCC" ]]; then
        err "找不到 nvcc，请通过 --cuda-home 指定 CUDA 路径"
        exit 1
    fi
    warn "使用 PATH 中的 nvcc: $NVCC"
else
    ok "找到 nvcc: $NVCC"
fi
CUDA_VERSION_FULL="$("$NVCC" --version | grep -oP 'release \K[0-9]+\.[0-9]+')"
log "CUDA 版本   : $CUDA_VERSION_FULL"

# 检查 Python / uv
PYTHON="$(command -v python 2>/dev/null || true)"
UV="$(command -v uv 2>/dev/null || true)"
if [[ -z "$PYTHON" ]]; then
    err "找不到 python，请激活 .sglang 虚拟环境后重试"
    exit 1
fi
ok "Python      : $("$PYTHON" --version)"

if [[ -n "$UV" ]]; then
    ok "uv          : $("$UV" --version)"
    # --no-build-isolation: 使用当前激活的 .sglang 环境中的 torch，
    # 避免孤立构建环境中的 torch wheel 缺少 cuda_cmake_macros.h 等生成头文件
    BUILD_CMD="$UV build --wheel --no-build-isolation"
else
    warn "未找到 uv，将使用 pip + build"
    BUILD_CMD="$PYTHON -m build --wheel --no-isolation"
fi

# 检查 ninja (加速 CMake 编译) — 必须使用 ninja
if command -v ninja &>/dev/null; then
    ok "ninja       : $(ninja --version)"
    export CMAKE_GENERATOR="Ninja"
else
    err "未找到 ninja！请先安装：uv pip install ninja  或  apt install ninja-build"
    exit 1
fi

sep

# ─── 导出编译环境变量 ────────────────────────────────────────────────────────
export CUDA_HOME
# /opt/cmake/bin 优先，使用系统 CMake（如已安装），其次才用 .sglang 里的
export PATH="/opt/cmake/bin:$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export MAX_JOBS="$JOBS"
# 限制 CMake/ninja 子进程并行度，减少内存峰值
export CMAKE_BUILD_PARALLEL_LEVEL=1
# CCCL 头文件路径（CUDA 13.0 aarch64）
export CPLUS_INCLUDE_PATH="/usr/local/cuda-13.0/targets/sbsa-linux/include/cccl${CPLUS_INCLUDE_PATH:+:$CPLUS_INCLUDE_PATH}"
# scikit-build-core 会自动读取 SKBUILD_CMAKE_ARGS
# CMAKE_POLICY_VERSION_MINIMUM=3.5 : CMake 4.x 移除了对旧版 cmake_minimum_required 的兼容，
#   dlpack (mscclpp 子依赖) 声明的是旧版本，需要此参数才能继续 configure。
SKBUILD_CMAKE_ARGS="-DCMAKE_CUDA_ARCHITECTURES=native;-DCMAKE_POLICY_VERSION_MINIMUM=3.5"

# ─── 预下载 FetchContent 依赖 ────────────────────────────────────────────
sep
log "预下载 FetchContent 依赖到本地目录: $DEPS_DIR"
log "(网络失败时自动重试，已下载的依赖下次直接跳过)"
DEPS_DIR="$DEPS_DIR" bash "$SCRIPT_DIR/fetch_sglang_deps.sh" | tee -a "$BUILD_LOG"

# 将各依赖本地路径注入 SKBUILD_CMAKE_ARGS，CMake 将完全第过网络下载
declare -A DEP_MAP=(
    ["repo-cutlass"]="cutlass"
    ["repo-deepgemm"]="DeepGEMM"
    ["repo-fmt"]="fmt"
    ["repo-triton"]="triton"
    ["repo-flashinfer"]="flashinfer"
    ["repo-flash-attention"]="sgl-attn"
    ["repo-mscclpp"]="mscclpp"
)
for cmake_name in "${!DEP_MAP[@]}"; do
    local_dir="${DEP_MAP[$cmake_name]}"
    dest="$DEPS_DIR/$local_dir"
    if [[ -d "$dest/.git" ]]; then
        var_name="FETCHCONTENT_SOURCE_DIR_$(echo "$cmake_name" | tr '[:lower:]-' '[:upper:]_')"
        SKBUILD_CMAKE_ARGS="${SKBUILD_CMAKE_ARGS};-D${var_name}=${dest}"
    fi
done
export SKBUILD_CMAKE_ARGS
log "SKBUILD_CMAKE_ARGS 已就绪，FETCHCONTENT 本地路径已设置"

# ─── Stage 1: sgl-kernel ─────────────────────────────────────────────────────
build_sgl_kernel() {
    sep
    log "Stage 1/2: 编译 sgl-kernel (CUDA 内核库)..."
    log "预计耗时: 15~40 分钟（取决于核心数和架构数量）"
    sep

    cd "$SRC_DIR/sgl-kernel"

    # 清理旧的构建产物（可选，保证干净构建）
    if [[ -d "dist" ]]; then
        warn "清理旧 dist/ 目录..."
        rm -rf dist/
    fi

    log "开始编译，实时输出如下（同时写入 $BUILD_LOG）..."
    echo "" | tee -a "$BUILD_LOG"

    # 用 unbuffer / stdbuf 保证实时输出不被缓冲
    if command -v stdbuf &>/dev/null; then
        stdbuf -oL -eL $BUILD_CMD . 2>&1 | tee -a "$BUILD_LOG"
    else
        $BUILD_CMD . 2>&1 | tee -a "$BUILD_LOG"
    fi

    # 收集 whl 文件
    local whl_count=0
    for whl in dist/*.whl; do
        [[ -f "$whl" ]] || continue
        cp -v "$whl" "$WHEELS_DIR/" | tee -a "$BUILD_LOG"
        whl_count=$((whl_count + 1))
    done

    if [[ $whl_count -eq 0 ]]; then
        err "sgl-kernel 编译完成但未找到 .whl 文件，请检查日志"
        exit 1
    fi
    ok "sgl-kernel 编译成功，共 $whl_count 个 whl 已复制到 $WHEELS_DIR/"
}

# ─── Stage 2: sglang Python 主包 ─────────────────────────────────────────────
build_sglang_python() {
    sep
    log "Stage 2/2: 打包 sglang Python 主包（纯 Python，速度快）..."
    sep

    cd "$SRC_DIR/python"

    if [[ -d "dist" ]]; then
        warn "清理旧 dist/ 目录..."
        rm -rf dist/
    fi

    log "开始打包..."
    echo "" | tee -a "$BUILD_LOG"

    if command -v stdbuf &>/dev/null; then
        stdbuf -oL -eL $BUILD_CMD . 2>&1 | tee -a "$BUILD_LOG"
    else
        $BUILD_CMD . 2>&1 | tee -a "$BUILD_LOG"
    fi

    local whl_count=0
    for whl in dist/*.whl; do
        [[ -f "$whl" ]] || continue
        cp -v "$whl" "$WHEELS_DIR/" | tee -a "$BUILD_LOG"
        whl_count=$((whl_count + 1))
    done

    if [[ $whl_count -eq 0 ]]; then
        err "sglang 打包完成但未找到 .whl 文件，请检查日志"
        exit 1
    fi
    ok "sglang 打包成功，共 $whl_count 个 whl 已复制到 $WHEELS_DIR/"
}

# ─── 执行构建 ────────────────────────────────────────────────────────────────
BUILD_START=$(date +%s)

[[ $SKIP_KERNEL -eq 0 ]] && build_sgl_kernel  || warn "已跳过 sgl-kernel 构建 (--skip-kernel)"
[[ $SKIP_SGLANG -eq 0 ]] && build_sglang_python || warn "已跳过 sglang 主包构建 (--skip-sglang)"

BUILD_END=$(date +%s)
ELAPSED=$(( BUILD_END - BUILD_START ))
ELAPSED_FMT="$(printf '%02d:%02d:%02d' $((ELAPSED/3600)) $(( (ELAPSED%3600)/60 )) $((ELAPSED%60)))"

# ─── 汇总输出 ────────────────────────────────────────────────────────────────
sep
ok "全部构建完成！总耗时: ${ELAPSED_FMT}"
sep
log "wheels/ 目录内容:"
ls -lh "$WHEELS_DIR"/*.whl 2>/dev/null | tee -a "$BUILD_LOG" || warn "wheels/ 目录暂无 whl 文件"
sep
log "完整构建日志: $BUILD_LOG"
sep

# ─── 安装提示 ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}安装方法:${RESET}"
echo -e "  ${CYAN}# 先安装内核库，再安装主包${RESET}"
echo -e "  uv pip install ${WHEELS_DIR}/sgl_kernel-*.whl"
echo -e "  uv pip install ${WHEELS_DIR}/sglang-*.whl --no-deps"
echo ""
