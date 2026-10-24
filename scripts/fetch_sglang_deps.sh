#!/usr/bin/env bash
# =============================================================================
# fetch_sglang_deps.sh
#
# 预先将 sgl-kernel 的所有 FetchContent 依赖 clone 到本地持久目录，
# 后续构建时通过 FETCHCONTENT_SOURCE_DIR_* 变量让 CMake 跳过网络下载。
#
# 用法:
#   ./scripts/fetch_sglang_deps.sh [--deps-dir <path>]
#
# 依赖目录默认: <repo_root>/sglang-deps/
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DEPS_DIR="${DEPS_DIR:-$REPO_ROOT/sglang-deps}"

# ─── 颜色 ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
log()  { echo -e "${CYAN}[$(date '+%H:%M:%S')] $*${RESET}"; }
ok()   { echo -e "${GREEN}[$(date '+%H:%M:%S')] ✓ $*${RESET}"; }
warn() { echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠ $*${RESET}"; }
err()  { echo -e "${RED}[$(date '+%H:%M:%S')] ✗ $*${RESET}"; }
sep()  { echo -e "${BOLD}$(printf '─%.0s' {1..72})${RESET}"; }

# ─── 参数 ────────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --deps-dir) DEPS_DIR="$2"; shift 2 ;;
        *) err "未知参数: $1"; exit 1 ;;
    esac
done

mkdir -p "$DEPS_DIR"
sep
echo -e "${BOLD}  sgl-kernel 依赖预下载${RESET}"
sep
log "依赖存储目录: $DEPS_DIR"
sep

# ─── 依赖列表 ────────────────────────────────────────────────────────────────
# 格式: "cmake名称|本地目录名|仓库URL|commit/tag"
declare -a DEPS=(
    "repo-cutlass|cutlass|https://github.com/NVIDIA/cutlass|57e3cfb47a2d9e0d46eb6335c3dc411498efa198"
    "repo-deepgemm|DeepGEMM|https://github.com/sgl-project/DeepGEMM|ffe2b6b97420a9f8c58268ca55755168e6e2f360"
    "repo-fmt|fmt|https://github.com/fmtlib/fmt|553ec11ec06fbe0beebfbb45f9dc3c9eabd83d28"
    "repo-triton|triton|https://github.com/triton-lang/triton|v3.5.1"
    "repo-flashinfer|flashinfer|https://github.com/flashinfer-ai/flashinfer|bc29697ba20b7e6bdb728ded98f04788e16ee021"
    "repo-flash-attention|sgl-attn|https://github.com/sgl-project/sgl-attn|f866ec34002250e74c8bbcbcffa0e1ae71300b2d"
    "repo-mscclpp|mscclpp|https://github.com/microsoft/mscclpp|51eca89d20f0cfb3764ccd764338d7b22cd486a6"
)

# ─── clone 函数（含重试 + blobless 部分克隆）────────────────────────────────
# blobless clone: 只下载 commit/tree 元数据，checkout 时按需拉取文件内容，
# 大幅减少每次 clone 的数据量，更抗网络抖动。
git_clone_with_retry() {
    local name="$1" url="$2" ref="$3" dest="$4"
    local max_tries=10
    local wait=5

    for i in $(seq 1 $max_tries); do
        log "[$name] 尝试 $i/$max_tries: clone $url @ $ref ..."
        if git clone --filter=blob:none --no-checkout "$url" "$dest" 2>&1; then
            log "[$name] checkout $ref ..."
            if git -C "$dest" checkout "$ref" 2>&1; then
                ok "[$name] 完成"
                return 0
            fi
        fi
        warn "[$name] 第 $i 次失败，${wait}s 后重试..."
        rm -rf "$dest"
        sleep "$wait"
        # 指数退避，最多等 60s
        wait=$(( wait < 60 ? wait * 2 : 60 ))
    done

    err "[$name] 经过 $max_tries 次尝试仍然失败，请检查网络或代理设置"
    return 1
}

# ─── 遍历并下载 ──────────────────────────────────────────────────────────────
FAILED=()
for dep in "${DEPS[@]}"; do
    IFS='|' read -r cmake_name local_dir url ref <<< "$dep"
    dest="$DEPS_DIR/$local_dir"

    if [[ -d "$dest/.git" ]]; then
        # 目录已存在，验证当前 HEAD
        current=$(git -C "$dest" rev-parse HEAD 2>/dev/null || echo "unknown")
        # 对于 tag 类型，解析为 commit hash 再比较
        expected=$(git -C "$dest" rev-parse "$ref^{}" 2>/dev/null || echo "$ref")
        if [[ "$current" == "$expected" || "$current" == "$ref" ]]; then
            ok "[$cmake_name] 已存在且 commit 正确，跳过"
            continue
        else
            warn "[$cmake_name] commit 不匹配 (当前: $current, 期望: $ref)，重新 clone..."
            rm -rf "$dest"
        fi
    fi

    if ! git_clone_with_retry "$cmake_name" "$url" "$ref" "$dest"; then
        FAILED+=("$cmake_name")
    fi
    echo ""
done

sep
if [[ ${#FAILED[@]} -gt 0 ]]; then
    err "以下依赖下载失败:"
    for f in "${FAILED[@]}"; do
        err "  - $f"
    done
    exit 1
fi

ok "所有依赖下载完成！"
sep

# ─── 打印供构建脚本使用的环境变量 ────────────────────────────────────────────
echo ""
echo -e "${BOLD}# 将以下变量加入构建环境（已由 build_sglang_whl.sh 自动处理）:${RESET}"
for dep in "${DEPS[@]}"; do
    IFS='|' read -r cmake_name local_dir url ref <<< "$dep"
    # CMake 将 FetchContent 名称转换规则: 转大写，- 替换为 _
    var_name="FETCHCONTENT_SOURCE_DIR_$(echo "$cmake_name" | tr '[:lower:]-' '[:upper:]_')"
    dest="$DEPS_DIR/$local_dir"
    echo "export ${var_name}=${dest}"
done
echo ""
