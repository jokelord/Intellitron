#!/usr/bin/env bash
# Safely inspect, snapshot, restore, and clear persistent SGLang compile caches.

set -Eeuo pipefail

PROGRAM_NAME="$(basename "$0")"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$PROJECT_ROOT/scripts/_workspace_paths.sh"
COMPILE_CACHE_BASE="$(expand_home_path "${SGLANG_COMPILE_CACHE_BASE:-$HOME}")"
FLASHINFER_BASE="$(expand_home_path "${FLASHINFER_WORKSPACE_BASE:-$COMPILE_CACHE_BASE}")"
FLASHINFER_CACHE_ROOT="$FLASHINFER_BASE/.cache/flashinfer"
SGLANG_CACHE_ROOT="$(expand_home_path "${SGLANG_CACHE_DIR:-$COMPILE_CACHE_BASE/.cache/sglang}")"
TORCH_EXTENSIONS_ROOT="$(expand_home_path "${TORCH_EXTENSIONS_DIR:-$COMPILE_CACHE_BASE/.cache/torch_extensions}")"
TRITON_CACHE_ROOT="$(expand_home_path "${TRITON_CACHE_DIR:-$COMPILE_CACHE_BASE/.triton/cache}")"
CUDA_CACHE_ROOT="$(expand_home_path "${CUDA_CACHE_PATH:-$COMPILE_CACHE_BASE/.nv/ComputeCache}")"
DEEPGEMM_CACHE_ROOT="$(expand_home_path "${SGLANG_DG_CACHE_DIR:-$COMPILE_CACHE_BASE/.cache/deep_gemm}")"
SNAPSHOT_ROOT="$(expand_home_path "${SGLANG_CACHE_SNAPSHOT_DIR:-$COMPILE_CACHE_BASE/.local/share/llm-deployment/sglang-cache-snapshots}")"
QUARANTINE_ROOT="$SNAPSHOT_ROOT/quarantine"

declare -a CACHE_LABELS=(
    flashinfer
    sglang
    torch_extensions
    triton
    cuda_compute
    deep_gemm
)
declare -a CACHE_PATHS=(
    "$FLASHINFER_CACHE_ROOT"
    "$SGLANG_CACHE_ROOT"
    "$TORCH_EXTENSIONS_ROOT"
    "$TRITON_CACHE_ROOT"
    "$CUDA_CACHE_ROOT"
    "$DEEPGEMM_CACHE_ROOT"
)

usage() {
    cat <<EOF
用法: $PROGRAM_NAME <命令> [参数]

命令:
  status
      显示所有 SGLang/FlashInfer 编译缓存的位置、大小和最近修改时间。
  verify
      校验 autotune JSON、共享库 ELF，并用 ninja -n 检查未完成/失效的构建。
  snapshot [名称]
      将全部缓存固化到独立快照（zstd 单线程压缩 + SHA-256）。
  list
      列出已完成快照和隔离区。
  restore <名称> --yes
      校验后恢复快照；当前缓存先移入可恢复隔离区。
  clear --scope <范围> --yes
      将目标缓存移入隔离区，让下次启动重新编译，不立即永久删除。
  purge-quarantine <隔离区名称> --yes
      永久删除指定隔离区。

clear 范围:
  flashinfer-current  当前 FlashInfer 版本的 JIT + SGLang autotune（推荐）
  flashinfer-all      所有 FlashInfer JIT + SGLang autotune 版本
  all                 全部已管理缓存（FlashInfer/SGLang/Torch/Triton/CUDA/DeepGEMM）

环境变量:
  SGLANG_COMPILE_CACHE_BASE  持久缓存基目录，默认: $HOME
  SGLANG_CACHE_SNAPSHOT_DIR  快照目录，默认: $SNAPSHOT_ROOT
  FLASHINFER_VERSION         覆盖自动检测到的当前 FlashInfer 版本

说明: clear/restore 前会拒绝在 SGLang、ninja、nvcc 或 cicc 运行时操作。
EOF
}

fail() {
    echo "错误: $*" >&2
    exit 1
}

human_size() {
    local path="$1"
    if [[ -e "$path" ]]; then
        du -sh -- "$path" 2>/dev/null | awk '{print $1}'
    else
        printf '%s\n' '-'
    fi
}

latest_mtime() {
    local path="$1"
    if [[ ! -e "$path" ]]; then
        printf '%s\n' '-'
        return
    fi
    find "$path" -type f -printf '%T@ %TY-%Tm-%Td %TH:%TM:%TS\n' 2>/dev/null \
        | awk '$1 > newest { newest=$1; value=substr($0, index($0, $2)) } END { print value }'
}

current_flashinfer_version() {
    if [[ -n "${FLASHINFER_VERSION:-}" ]]; then
        printf '%s\n' "$FLASHINFER_VERSION"
        return
    fi

    local python_bin="$PROJECT_ROOT/.sglang/bin/python"
    if [[ -x "$python_bin" ]]; then
        "$python_bin" -c \
            'import importlib.metadata; print(importlib.metadata.version("flashinfer-python"))' \
            2>/dev/null && return
    fi

    local newest
    newest="$(find "$FLASHINFER_CACHE_ROOT" -mindepth 1 -maxdepth 1 -type d \
        -printf '%f\n' 2>/dev/null | sort -V | tail -n 1 || true)"
    [[ -n "$newest" ]] || fail "无法检测 FlashInfer 版本；请设置 FLASHINFER_VERSION"
    printf '%s\n' "$newest"
}

active_processes() {
    ps -eo pid=,comm=,args= 2>/dev/null \
        | awk -v self="$$" '
            $1 != self &&
            ($2 ~ /^(ninja|nvcc|cicc)$/ || $2 ~ /^sglang::/ ||
             ($0 ~ /python/ && $0 ~ /sglang\.launch_server/)) { print }
        ' || true
}

require_idle() {
    local processes
    processes="$(active_processes)"
    if [[ -n "$processes" ]]; then
        echo "检测到 SGLang/编译进程，拒绝修改缓存:" >&2
        printf '%s\n' "$processes" >&2
        fail "请先停止 SGLang 并等待 nvcc/cicc/ninja 全部退出"
    fi
}

require_yes() {
    [[ "${1:-}" == "--yes" ]] || fail "该操作会替换或移走缓存；确认后追加 --yes"
}

validate_managed_path() {
    local candidate="$1"
    local allowed=0
    local path

    [[ -n "$candidate" && "$candidate" == /* ]] || fail "缓存路径必须是绝对路径: $candidate"
    [[ "$candidate" != "/" && "$candidate" != "$HOME" && "$candidate" != "$COMPILE_CACHE_BASE" ]] \
        || fail "拒绝操作过宽路径: $candidate"

    for path in "${CACHE_PATHS[@]}"; do
        if [[ "$candidate" == "$path" || "$candidate" == "$path"/* ]]; then
            allowed=1
            break
        fi
    done
    (( allowed == 1 )) || fail "路径不属于受管缓存: $candidate"
}

show_status() {
    local i path count
    printf '%-18s %-9s %-10s %-26s %s\n' "CACHE" "EXISTS" "SIZE" "LATEST" "PATH"
    for i in "${!CACHE_LABELS[@]}"; do
        path="${CACHE_PATHS[$i]}"
        if [[ -e "$path" ]]; then
            count="yes"
        else
            count="no"
        fi
        printf '%-18s %-9s %-10s %-26s %s\n' \
            "${CACHE_LABELS[$i]}" "$count" "$(human_size "$path")" \
            "$(latest_mtime "$path")" "$path"
    done
    echo
    echo "FlashInfer 当前版本: $(current_flashinfer_version)"
    echo "快照目录: $SNAPSHOT_ROOT"
    echo "当前 shell 编译并发: MAX_JOBS=${MAX_JOBS:-unset}, TRITON_NUM_COMPILE_THREADS=${TRITON_NUM_COMPILE_THREADS:-unset}"
    echo "NVFP4 低内存建议值: MAX_JOBS=1, TRITON_NUM_COMPILE_THREADS=1"

    local processes
    processes="$(active_processes)"
    if [[ -n "$processes" ]]; then
        echo
        echo "当前相关进程:"
        printf '%s\n' "$processes"
    fi
}

verify_json_files() {
    local bad=0 json_file
    while IFS= read -r -d '' json_file; do
        if ! python3 - "$json_file" <<'PY' >/dev/null 2>&1
import json
import sys
with open(sys.argv[1], "rb") as handle:
    json.load(handle)
PY
        then
            echo "损坏 JSON: $json_file" >&2
            bad=1
        fi
    done < <(find "$SGLANG_CACHE_ROOT" -type f -name '*.json' -print0 2>/dev/null)
    return "$bad"
}

verify_shared_libraries() {
    local version current_root bad=0 so_file
    version="$(current_flashinfer_version)"
    current_root="$FLASHINFER_CACHE_ROOT/$version"
    while IFS= read -r -d '' so_file; do
        if [[ ! -s "$so_file" ]] || ! readelf -h "$so_file" >/dev/null 2>&1; then
            echo "损坏或不完整共享库: $so_file" >&2
            bad=1
        fi
    done < <(find "$current_root" "$TORCH_EXTENSIONS_ROOT" \
        -type f -name '*.so' -print0 2>/dev/null)
    return "$bad"
}

report_pending_ninja_work() {
    local version current_root build_file build_dir output step_summary pending=0
    version="$(current_flashinfer_version)"
    current_root="$FLASHINFER_CACHE_ROOT/$version"
    while IFS= read -r -d '' build_file; do
        build_dir="$(dirname "$build_file")"
        if ! output="$(ninja -C "$build_dir" -n 2>&1)"; then
            echo "Ninja dry-run 失败: $build_dir" >&2
            printf '%s\n' "$output" | tail -n 5 >&2
            pending=1
        elif [[ "$output" != *"no work to do."* ]]; then
            step_summary="$(printf '%s\n' "$output" \
                | awk '/^\[[0-9]+\/[0-9]+\]/ { last=$1 } END { print last }')"
            echo "待编译/失效构建 ${step_summary:-'(任务数未知)'}: $build_dir"
            pending=1
        fi
    done < <(find "$current_root" "$TORCH_EXTENSIONS_ROOT" \
        -type f -name build.ninja -print0 2>/dev/null)
    return "$pending"
}

verify_caches() {
    local corrupt=0 pending=0
    command -v readelf >/dev/null 2>&1 || fail "缺少 readelf"
    command -v ninja >/dev/null 2>&1 || fail "缺少 ninja"

    echo "正在校验 autotune JSON..."
    verify_json_files || corrupt=1
    echo "正在校验 FlashInfer/Torch 共享库 ELF..."
    verify_shared_libraries || corrupt=1
    echo "正在 dry-run 检查 Ninja 构建状态（不会编译）..."
    report_pending_ninja_work || pending=1

    if (( corrupt == 1 )); then
        fail "发现损坏产物；建议先 snapshot，再 clear --scope flashinfer-current --yes"
    fi
    if (( pending == 1 )); then
        echo "校验结果: 文件格式正常，但存在未完成或因依赖/命令变化而失效的构建。"
        echo "这不是 JSON 过期；下次启动仍会编译。请使用 MAX_JOBS=1 低并发续编。"
    else
        echo "校验结果: 未发现损坏文件或待执行 Ninja 任务。"
    fi
}

snapshot_extension() {
    if command -v zstd >/dev/null 2>&1; then
        printf '%s\n' '.tar.zst'
    else
        printf '%s\n' '.tar.gz'
    fi
}

create_archive() {
    local source_path="$1"
    local archive_path="$2"
    local parent base
    parent="$(dirname "$source_path")"
    base="$(basename "$source_path")"
    if [[ "$archive_path" == *.tar.zst ]]; then
        tar -C "$parent" -I 'zstd -T1 -3' -cf "$archive_path" -- "$base"
    else
        tar -C "$parent" -czf "$archive_path" -- "$base"
    fi
}

extract_archive() {
    local archive_path="$1"
    local target_parent="$2"
    if [[ "$archive_path" == *.tar.zst ]]; then
        tar -C "$target_parent" -I zstd -xf "$archive_path"
    else
        tar -C "$target_parent" -xzf "$archive_path"
    fi
}

create_snapshot() {
    local requested_name="${1:-}"
    local timestamp name temp_dir final_dir extension i path archive filename
    require_idle
    timestamp="$(date +%Y%m%d_%H%M%S)"
    name="${requested_name:-cache_$timestamp}"
    [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || fail "快照名称只能包含字母、数字、点、下划线和短横线"
    final_dir="$SNAPSHOT_ROOT/$name"
    [[ ! -e "$final_dir" ]] || fail "快照已存在: $final_dir"

    mkdir -p "$SNAPSHOT_ROOT"
    temp_dir="$(mktemp -d "$SNAPSHOT_ROOT/.snapshot-${name}.XXXXXX")"
    trap 'rm -rf -- "$temp_dir"' RETURN
    extension="$(snapshot_extension)"
    : >"$temp_dir/paths.tsv"

    for i in "${!CACHE_LABELS[@]}"; do
        path="${CACHE_PATHS[$i]}"
        [[ -e "$path" ]] || continue
        filename="${CACHE_LABELS[$i]}$extension"
        archive="$temp_dir/$filename"
        echo "固化 ${CACHE_LABELS[$i]}: $path"
        create_archive "$path" "$archive"
        printf '%s\t%s\t%s\n' "${CACHE_LABELS[$i]}" "$path" "$filename" >>"$temp_dir/paths.tsv"
    done

    [[ -s "$temp_dir/paths.tsv" ]] || fail "没有可固化的缓存"
    (
        cd "$temp_dir"
        sha256sum -- *"$extension" >SHA256SUMS
    )
    {
        echo "created_at=$(date --iso-8601=seconds)"
        echo "host=$(hostname)"
        echo "flashinfer_version=$(current_flashinfer_version)"
        echo "max_jobs=${MAX_JOBS:-unset}"
        echo "triton_num_compile_threads=${TRITON_NUM_COMPILE_THREADS:-unset}"
    } >"$temp_dir/metadata.txt"
    touch "$temp_dir/COMPLETE"
    mv -- "$temp_dir" "$final_dir"
    trap - RETURN
    echo "快照完成: $final_dir"
}

list_snapshots() {
    local dir found=0
    echo "已完成快照:"
    if [[ -d "$SNAPSHOT_ROOT" ]]; then
        while IFS= read -r -d '' dir; do
            [[ -f "$dir/COMPLETE" ]] || continue
            printf '  %s  (%s)\n' "$(basename "$dir")" "$(human_size "$dir")"
            found=1
        done < <(find "$SNAPSHOT_ROOT" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
    fi
    (( found == 1 )) || echo "  (无)"

    echo "隔离区:"
    found=0
    if [[ -d "$QUARANTINE_ROOT" ]]; then
        while IFS= read -r -d '' dir; do
            printf '  %s  (%s)\n' "$(basename "$dir")" "$(human_size "$dir")"
            found=1
        done < <(find "$QUARANTINE_ROOT" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
    fi
    (( found == 1 )) || echo "  (无)"
}

move_to_quarantine() {
    local source_path="$1"
    local label="$2"
    local quarantine_dir="$3"
    local destination="$quarantine_dir/$label"
    validate_managed_path "$source_path"
    [[ -e "$source_path" ]] || return 0
    mkdir -p "$quarantine_dir"
    [[ ! -e "$destination" ]] || fail "隔离目标已存在: $destination"
    mv -- "$source_path" "$destination"
    printf '%s\t%s\n' "$label" "$source_path" >>"$quarantine_dir/original-paths.tsv"
}

clear_cache() {
    local scope='' confirmation='' version timestamp quarantine_dir i
    shift
    while (( $# > 0 )); do
        case "$1" in
            --scope)
                (( $# >= 2 )) || fail "--scope 缺少值"
                scope="$2"
                shift 2
                ;;
            --yes)
                confirmation='--yes'
                shift
                ;;
            *) fail "未知参数: $1" ;;
        esac
    done
    require_yes "$confirmation"
    require_idle
    version="$(current_flashinfer_version)"
    timestamp="clear_$(date +%Y%m%d_%H%M%S)_$scope"
    quarantine_dir="$QUARANTINE_ROOT/$timestamp"

    case "$scope" in
        flashinfer-current)
            move_to_quarantine "$FLASHINFER_CACHE_ROOT/$version" "flashinfer-$version" "$quarantine_dir"
            move_to_quarantine "$SGLANG_CACHE_ROOT/flashinfer/autotune/$version" \
                "sglang-autotune-$version" "$quarantine_dir"
            ;;
        flashinfer-all)
            move_to_quarantine "$FLASHINFER_CACHE_ROOT" "flashinfer" "$quarantine_dir"
            move_to_quarantine "$SGLANG_CACHE_ROOT/flashinfer/autotune" \
                "sglang-flashinfer-autotune" "$quarantine_dir"
            ;;
        all)
            for i in "${!CACHE_LABELS[@]}"; do
                move_to_quarantine "${CACHE_PATHS[$i]}" "${CACHE_LABELS[$i]}" "$quarantine_dir"
            done
            ;;
        *) fail "无效 scope: ${scope:-<空>}" ;;
    esac

    if [[ -s "$quarantine_dir/original-paths.tsv" ]]; then
        echo "缓存已移入隔离区（下次启动会重新编译）: $quarantine_dir"
        echo "确认新缓存可用后，可用 purge-quarantine 永久删除它。"
    else
        rmdir "$quarantine_dir" 2>/dev/null || true
        echo "目标缓存不存在，无需清理。"
    fi
}

restore_snapshot() {
    local name="${1:-}" confirmation="${2:-}" snapshot_dir quarantine_dir
    local label target archive_name archive_path parent
    [[ -n "$name" ]] || fail "restore 需要快照名称"
    require_yes "$confirmation"
    require_idle
    [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || fail "无效快照名称"
    snapshot_dir="$SNAPSHOT_ROOT/$name"
    [[ -f "$snapshot_dir/COMPLETE" && -s "$snapshot_dir/paths.tsv" ]] \
        || fail "快照不存在或未完成: $snapshot_dir"
    (
        cd "$snapshot_dir"
        sha256sum -c SHA256SUMS
    ) || fail "快照 SHA-256 校验失败"

    quarantine_dir="$QUARANTINE_ROOT/restore_$(date +%Y%m%d_%H%M%S)_$name"
    while IFS=$'\t' read -r label target archive_name; do
        validate_managed_path "$target"
        archive_path="$snapshot_dir/$archive_name"
        [[ -f "$archive_path" ]] || fail "快照缺少归档: $archive_path"
        if [[ -e "$target" ]]; then
            move_to_quarantine "$target" "$label" "$quarantine_dir"
        fi
        parent="$(dirname "$target")"
        mkdir -p "$parent"
        echo "恢复 $label -> $target"
        extract_archive "$archive_path" "$parent"
        [[ -e "$target" ]] || fail "恢复后未找到目标: $target"
    done <"$snapshot_dir/paths.tsv"
    echo "快照已恢复: $name"
    [[ -d "$quarantine_dir" ]] && echo "原缓存保留于: $quarantine_dir"
}

purge_quarantine() {
    local name="${1:-}" confirmation="${2:-}" target
    [[ -n "$name" ]] || fail "purge-quarantine 需要隔离区名称"
    require_yes "$confirmation"
    require_idle
    [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || fail "无效隔离区名称"
    target="$QUARANTINE_ROOT/$name"
    [[ -d "$target" ]] || fail "隔离区不存在: $target"
    [[ "$(dirname "$target")" == "$QUARANTINE_ROOT" ]] || fail "拒绝删除非隔离区路径"
    rm -rf -- "$target"
    echo "已永久删除隔离区: $target（不可恢复）"
}

command_name="${1:-}"
case "$command_name" in
    status) show_status ;;
    verify) verify_caches ;;
    snapshot) create_snapshot "${2:-}" ;;
    list) list_snapshots ;;
    restore) restore_snapshot "${2:-}" "${3:-}" ;;
    clear) clear_cache "$@" ;;
    purge-quarantine) purge_quarantine "${2:-}" "${3:-}" ;;
    help|-h|--help|'') usage ;;
    *) usage >&2; fail "未知命令: $command_name" ;;
esac
