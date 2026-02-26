#!/usr/bin/env python3
"""
强制清理 UMA 内存（适用于 Jetson Thor 等统一内存架构）
用于清理因程序异常退出而残留的 Python/vLLM 进程
"""

import os
import sys
import subprocess
import time
import psutil

def find_python_processes():
    """查找所有占用大量内存的 Python 进程（UMA 特定）"""
    processes = []
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'memory_info']):
            try:
                # 查找 Python 进程
                if proc.info['name'] and 'python' in proc.info['name'].lower():
                    mem_mb = proc.info['memory_info'].rss / 1024 / 1024
                    
                    # 只显示占用超过 100MB 的进程
                    if mem_mb > 100:
                        cmdline = ' '.join(proc.info['cmdline'] or [])
                        # 截断过长的命令行
                        if len(cmdline) > 80:
                            cmdline = cmdline[:77] + '...'
                        
                        processes.append({
                            'pid': proc.info['pid'],
                            'name': proc.info['name'],
                            'memory_mb': mem_mb,
                            'cmdline': cmdline
                        })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        # 按内存使用量排序
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


def cleanup_system_cache():
    """清理系统缓存（需要 root 权限）"""
    try:
        print("\n🧹 尝试清理系统缓存...")
        # 注意：这需要 root 权限
        result = subprocess.run(
            ['sudo', 'sync'],
            capture_output=True,
            timeout=5
        )
        
        result = subprocess.run(
            ['sudo', 'sh', '-c', 'echo 3 > /proc/sys/vm/drop_caches'],
            capture_output=True,
            timeout=5
        )
        
        if result.returncode == 0:
            print("✅ 系统缓存已清理")
        else:
            print("⚠️  需要 sudo 权限来清理系统缓存")
    except subprocess.TimeoutExpired:
        print("⚠️  清理缓存超时")
    except Exception as e:
        print(f"⚠️  清理系统缓存失败: {e}")


def cleanup_pytorch_cache():
    """清理 PyTorch CUDA 缓存"""
    try:
        import torch
        import gc
        
        if not torch.cuda.is_available():
            print("[INFO] CUDA 不可用，跳过 PyTorch 清理")
            return
        
        print(f"\n🔥 检测到 CUDA 可用")
        
        # 执行清理
        print("🧹 清理 PyTorch CUDA 缓存...")
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
        
        print("✅ PyTorch CUDA 缓存已清理")
        
    except ImportError:
        print("[INFO] PyTorch 未安装，跳过清理")
    except Exception as e:
        print(f"[WARN] 清理 CUDA 缓存失败: {e}")


def kill_python_processes(force=False, exclude_current=True):
    """终止占用大量内存的 Python 进程（UMA 特定）"""
    processes = find_python_processes()
    
    if not processes:
        print("\n[INFO] 未发现占用大量内存的 Python 进程")
        return
    
    # 排除当前进程
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
        # 等待进程退出
        print("\n⏳ 等待进程退出...")
        time.sleep(3)
        
        # 检查是否还有进程
        remaining = find_python_processes()
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
        description="清理 UMA 内存和终止占用内存的 Python 进程（Jetson Thor 专用）"
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='强制终止所有占用大量内存的 Python 进程'
    )
    parser.add_argument(
        '--clear-cache',
        action='store_true',
        help='清理系统缓存（需要 sudo 权限）'
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
    
    # 显示内存状态
    show_memory_status()
    
    if args.status:
        return
    
    # 1. 清理 PyTorch CUDA 缓存
    cleanup_pytorch_cache()
    
    # 2. 查找并处理 Python 进程
    kill_python_processes(force=args.force)
    
    # 3. 清理系统缓存（如果请求）
    if args.clear_cache:
        cleanup_system_cache()
    
    # 4. 显示最终状态
    print("\n⏳ 等待内存释放...")
    time.sleep(2)
    show_memory_status()
    
    print("\n" + "=" * 80)
    print("✅ 清理完成！")
    print("=" * 80)
    print("\n💡 提示:")
    print("   - 如果内存仍然被占用，使用 --force 参数强制终止进程")
    print("   - 使用 --clear-cache 清理系统缓存（需要 sudo 权限）")
    print("   - 某些内存可能被系统保留，这是正常的")


if __name__ == "__main__":
    main()
