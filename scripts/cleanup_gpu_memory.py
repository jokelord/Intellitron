#!/usr/bin/env python3
"""
强制清理 UMA 内存（适用于 Jetson Thor 等统一内存架构）
用于清理因程序异常退出而残留的 SGLang/vLLM 进程和 NGC 容器。

默认只终止项目相关的 Python 进程（sglang / vllm / backend.main），
以及镜像名或容器名包含 sglang / vllm 的 Docker 容器。
使用 --include-all-python 可恢复旧行为（终止所有 >100MB 的 Python 进程，慎用）。
"""

import os
import sys
import subprocess
import time
import shutil

try:
    import psutil
except ImportError:
    print("[ERROR] psutil 未安装，请运行: pip install psutil")
    sys.exit(1)

# 项目相关的 Python 进程命令行匹配模式（小写子串匹配）
DEFAULT_PROCESS_PATTERNS = [
    "sglang",
    "vllm",
    "backend.main",
]

# 需要停止的 Docker 容器匹配模式（匹配容器名或镜像名，小写子串）
DEFAULT_CONTAINER_PATTERNS = [
    "vllm",
    "sglang",
]

# Python 进程最小内存阈值（MB），低于此值的进程忽略
MIN_MEMORY_MB = 100


def find_python_processes(patterns=None, include_all=False):
    """查找占用大量内存的 Python 进程。

    Args:
        patterns: 命令行匹配模式列表（小写子串匹配）。None 表示使用默认模式。
        include_all: True 时忽略 patterns，返回所有 >MIN_MEMORY_MB 的 Python 进程。
    """
    if patterns is None:
        patterns = DEFAULT_PROCESS_PATTERNS

    processes = []
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'memory_info']):
            try:
                if not (proc.info['name'] and 'python' in proc.info['name'].lower()):
                    continue

                mem_mb = proc.info['memory_info'].rss / 1024 / 1024
                if mem_mb < MIN_MEMORY_MB:
                    continue

                cmdline = ' '.join(proc.info['cmdline'] or [])
                cmdline_lower = cmdline.lower()

                if not include_all:
                    if not any(p in cmdline_lower for p in patterns):
                        continue

                display = cmdline[:77] + '...' if len(cmdline) > 80 else cmdline
                processes.append({
                    'pid': proc.info['pid'],
                    'name': proc.info['name'],
                    'memory_mb': mem_mb,
                    'cmdline': display,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        processes.sort(key=lambda x: x['memory_mb'], reverse=True)
        return processes
    except Exception as e:
        print(f"[ERROR] 查找进程失败: {e}")
        return []


def show_memory_status():
    """显示 UMA 内存状态"""
    try:
        mem = psutil.virtual_memory()
        print("\n" + "=" * 80)
        print("🖥️  UMA 内存状态")
        print("=" * 80)
        print(f"总内存:   {mem.total / 1024**3:.1f} GB")
        print(f"已使用:   {mem.used / 1024**3:.1f} GB ({mem.percent:.1f}%)")
        print(f"可用:     {mem.available / 1024**3:.1f} GB")
        print(f"缓存:     {(mem.cached + mem.buffers) / 1024**3:.1f} GB")
        print("=" * 80)
    except Exception as e:
        print(f"[ERROR] 获取内存状态失败: {e}")


def cleanup_pytorch_cache():
    """清理当前进程的 PyTorch CUDA 缓存。

    注意：torch.cuda.empty_cache() 只能释放调用进程自身的缓存分配器，
    无法清理其他进程的 CUDA 显存。从 cleanup 脚本调用时，当前进程没有
    CUDA 分配，此操作是 no-op；保留它是为了 standalone 调用场景。
    """
    try:
        import torch
        import gc

        if not torch.cuda.is_available():
            print("[INFO] CUDA 不可用，跳过 PyTorch 清理")
            return

        print("\n🔥 检测到 CUDA 可用（仅清理当前进程缓存，不影响其他进程）")
        print("🧹 清理 PyTorch CUDA 缓存...")
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
        print("✅ PyTorch CUDA 缓存已清理")

    except ImportError:
        print("[INFO] PyTorch 未安装，跳过清理")
    except Exception as e:
        print(f"[WARN] 清理 CUDA 缓存失败: {e}")


def stop_docker_containers(patterns=None):
    """停止并移除相关的 Docker 容器（sglang / vllm NGC 容器）。

    Args:
        patterns: 容器名/镜像名匹配模式列表。None 表示使用默认模式。
    """
    if patterns is None:
        patterns = DEFAULT_CONTAINER_PATTERNS

    docker_bin = shutil.which("docker")
    if not docker_bin:
        print("[INFO] Docker 未安装，跳过容器清理")
        return

    try:
        result = subprocess.run(
            [docker_bin, "info"],
            capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            print("[INFO] Docker daemon 未运行，跳过容器清理")
            return
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("[INFO] Docker daemon 不可用，跳过容器清理")
        return

    try:
        result = subprocess.run(
            [docker_bin, "ps", "--format",
             "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print(f"[WARN] 无法列出容器: {result.stderr.strip()}")
            return
    except subprocess.TimeoutExpired:
        print("[WARN] docker ps 超时，跳过容器清理")
        return

    containers = []
    for line in result.stdout.strip().splitlines():
        parts = line.split('\t')
        if len(parts) < 4:
            continue
        container_id, name, image, status = parts[0], parts[1], parts[2], parts[3]
        name_lower = name.lower()
        image_lower = image.lower()
        if any(p in name_lower or p in image_lower for p in patterns):
            containers.append({
                'id': container_id,
                'name': name,
                'image': image,
                'status': status,
            })

    if not containers:
        print("\n[INFO] 未发现相关 Docker 容器（sglang/vllm）")
        return

    print(f"\n📦 发现 {len(containers)} 个相关 Docker 容器:")
    print(f"{'容器ID':<14} {'名称':<30} {'镜像'}")
    print("-" * 80)
    for c in containers:
        print(f"{c['id']:<14} {c['name']:<30} {c['image']}")

    print("\n🛑 正在停止并移除容器...")
    stopped_count = 0
    for c in containers:
        try:
            result = subprocess.run(
                [docker_bin, "rm", "-f", c['id']],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                print(f"  ✓ 已移除容器 {c['name']} ({c['id']})")
                stopped_count += 1
            else:
                print(f"  ✗ 移除容器失败: {c['name']} ({c['id']}) - {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print(f"  ✗ 移除容器超时: {c['name']} ({c['id']})")
        except Exception as e:
            print(f"  ✗ 无法移除容器 {c['name']}: {e}")

    if stopped_count > 0:
        print(f"\n⏳ 等待容器资源释放...")
        time.sleep(2)


def kill_python_processes(force=False, patterns=None, include_all=False, exclude_current=True):
    """终止项目相关的 Python 进程。"""
    processes = find_python_processes(patterns=patterns, include_all=include_all)

    if not processes:
        scope = "所有" if include_all else "项目相关"
        print(f"\n[INFO] 未发现{scope}占用大量内存的 Python 进程")
        return

    current_pid = os.getpid()
    if exclude_current:
        processes = [p for p in processes if p['pid'] != current_pid]

    if not processes:
        print("\n[INFO] 未发现其他占用内存的 Python 进程")
        return

    print(f"\n📊 发现 {len(processes)} 个占用大量内存的 Python 进程:")
    print(f"{'PID':<8} {'内存(MB)':<12} {'命令行'}")
    print("-" * 80)
    for p in processes:
        print(f"{p['pid']:<8} {p['memory_mb']:<12.1f} {p['cmdline']}")

    if not force:
        print("\n⚠️  使用 --force 参数来终止这些进程")
        print("   示例: python cleanup_gpu_memory.py --force")
        return

    print("\n⚠️  正在终止 Python 进程...")
    killed_count = 0
    for p in processes:
        try:
            os.kill(p['pid'], 15)  # SIGTERM
            print(f"  ✓ 已发送终止信号到进程 {p['pid']} ({p['memory_mb']:.1f}MB)")
            killed_count += 1
        except Exception as e:
            print(f"  ✗ 无法终止进程 {p['pid']}: {e}")

    if killed_count > 0:
        print("\n⏳ 等待进程退出...")
        time.sleep(3)

        remaining = find_python_processes(patterns=patterns, include_all=include_all)
        if exclude_current:
            remaining = [p for p in remaining if p['pid'] != current_pid]

        if remaining:
            print("\n⚠️  部分进程未响应，尝试强制终止...")
            for p in remaining:
                try:
                    os.kill(p['pid'], 9)  # SIGKILL
                    print(f"  ✓ 已强制终止进程 {p['pid']}")
                except Exception as e:
                    print(f"  ✗ 无法强制终止进程 {p['pid']}: {e}")

            time.sleep(1)

        print(f"\n✅ 已终止 {killed_count} 个进程")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="清理 UMA 内存：终止 SGLang/vLLM 进程和 NGC 容器（Jetson Thor 专用）"
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='强制终止项目相关的 Python 进程（sglang/vllm/backend）'
    )
    parser.add_argument(
        '--include-all-python',
        action='store_true',
        help='终止所有占用大量内存的 Python 进程（不限于项目相关，慎用：会杀掉 IDE/Jupyter 等）'
    )
    parser.add_argument(
        '--no-containers',
        action='store_true',
        help='跳过 Docker 容器清理'
    )
    parser.add_argument(
        '--status',
        action='store_true',
        help='仅显示内存状态，不执行清理'
    )

    args = parser.parse_args()

    print("=" * 80)
    print("🖥️  UMA 内存清理工具（Jetson Thor）")
    print("=" * 80)

    if args.include_all_python:
        print("\n⚠️  --include-all-python 已启用：将终止所有 >100MB 的 Python 进程")
        print("   这可能影响 IDE、Jupyter 等无关进程，请确认后继续。")

    show_memory_status()

    if args.status:
        return

    # 1. 清理 PyTorch CUDA 缓存（仅当前进程，对其他进程无效）
    cleanup_pytorch_cache()

    # 2. 停止相关 Docker 容器（sglang/vllm NGC 容器）
    if not args.no_containers:
        stop_docker_containers()

    # 3. 查找并处理 Python 进程
    kill_python_processes(
        force=args.force,
        include_all=args.include_all_python,
    )

    # 4. 显示最终状态
    print("\n⏳ 等待内存释放...")
    time.sleep(2)
    show_memory_status()

    print("\n" + "=" * 80)
    print("✅ 清理完成！")
    print("=" * 80)
    print("\n💡 提示:")
    print("   - 使用 --force 强制终止项目相关 Python 进程")
    print("   - 使用 --include-all-python 终止所有 Python 进程（慎用）")
    print("   - 使用 --no-containers 跳过 Docker 容器清理")
    print("   - 系统缓存清理请使用 0_force_cleanup_uma.sh 脚本")


if __name__ == "__main__":
    main()
