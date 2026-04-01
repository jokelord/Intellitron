"""
系统命令执行工具 (System Operations)

提供受安全黑名单保护的 shell 命令执行功能。
黑名单模式：阻断高危命令（rm -rf /、sudo 等），其余放行。
"""

import re
import subprocess
from typing import Optional

_DEFAULT_DANGEROUS_PATTERNS = [
    (r"rm\s+.*-[a-z]*r[a-z]*f[a-z]*\s+/(?:\s|$)", "递归删除根目录"),
    (r"rm\s+.*-[a-z]*f[a-z]*r[a-z]*\s+/(?:\s|$)", "递归删除根目录"),
    (r"\bsudo\s+", "提权命令"),
    (r"\bmkfs\b", "格式化设备"),
    (r"\bdd\s+.*of=/dev/", "直接写入设备"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "Fork Bomb"),
    (r"\b(shutdown|reboot|poweroff|halt)\b", "系统关机/重启"),
    (r"\bchmod\s+[0-7]*7[0-7]*\s+/(?:\s|$)", "根目录权限扩大"),
    (r"\bcurl\s+.*\|\s*(ba)?sh\b", "远程脚本执行"),
    (r"\bwget\s+.*\|\s*(ba)?sh\b", "远程脚本执行"),
    (r">\s*/dev/sd[a-z]", "直接写入磁盘设备"),
    (r"\bsystemctl\s+(stop|disable|mask)\s+", "禁用系统服务"),
]

_compiled_patterns: list = [
    (re.compile(pattern, re.IGNORECASE), desc)
    for pattern, desc in _DEFAULT_DANGEROUS_PATTERNS
]


def _load_blacklist_from_config():
    """从配置文件加载黑名单正则，不存在时使用默认黑名单"""
    global _compiled_patterns
    try:
        from backend.config import get_config

        config = get_config()
        blacklist = config.agent.command_blacklist
        if blacklist:
            _compiled_patterns = [
                (re.compile(item["pattern"], re.IGNORECASE), item["reason"])
                for item in blacklist
            ]
        else:
            _compiled_patterns = [
                (re.compile(p, re.IGNORECASE), d)
                for p, d in _DEFAULT_DANGEROUS_PATTERNS
            ]
    except Exception:
        _compiled_patterns = [
            (re.compile(p, re.IGNORECASE), d) for p, d in _DEFAULT_DANGEROUS_PATTERNS
        ]


_load_blacklist_from_config()


def _check_command_safety(command: str) -> str | None:
    """
    检查命令是否命中黑名单。

    Returns:
        None 表示安全，否则返回阻断原因
    """
    for pattern, description in _compiled_patterns:
        if pattern.search(command):
            return description
    return None


def exec_command(command: str, timeout: int = 60, check_abort: callable = None) -> str:
    """
    执行 shell 命令，受黑名单安全保护。

    Args:
        command: 要执行的 shell 命令
        timeout: 超时时间（秒），防止死循环卡死
        check_abort: 可选的中止检查回调，返回 True 表示已被中止

    Returns:
        命令输出结果（stdout + stderr），或安全阻断/错误信息
    """
    if check_abort and check_abort():
        return "命令已被用户中止"

    blocked_reason = _check_command_safety(command)
    if blocked_reason:
        return f"命令被安全策略阻断: {blocked_reason}\n被阻断的命令: {command}"

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=None,
        )

        import select
        import time

        start_time = time.time()
        stdout_chunks = []
        stderr_chunks = []

        while True:
            if check_abort and check_abort():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return "命令已被用户中止"

            elapsed = time.time() - start_time
            remaining = timeout - elapsed
            if remaining <= 0:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return f"命令执行超时 ({timeout} 秒): {command}"

            reads = []
            if proc.stdout:
                reads.append(proc.stdout)
            if proc.stderr:
                reads.append(proc.stderr)

            if not reads:
                break

            readable, _, _ = select.select(reads, [], [], min(1, remaining))

            if not readable:
                if proc.poll() is not None:
                    break
                continue

            for stream in readable:
                if stream == proc.stdout:
                    chunk = stream.read(4096)
                    if chunk:
                        stdout_chunks.append(chunk)
                elif stream == proc.stderr:
                    chunk = stream.read(4096)
                    if chunk:
                        stderr_chunks.append(chunk)

            if proc.poll() is not None:
                # 进程已退出，排空管道中剩余数据
                if proc.stdout:
                    remaining_out = proc.stdout.read()
                    if remaining_out:
                        stdout_chunks.append(remaining_out)
                if proc.stderr:
                    remaining_err = proc.stderr.read()
                    if remaining_err:
                        stderr_chunks.append(remaining_err)
                break

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)

        output_parts = []
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.append(f"[stderr]\n{stderr}")
        if proc.returncode != 0:
            output_parts.append(f"[退出码: {proc.returncode}]")

        return "\n".join(output_parts) if output_parts else "(无输出)"

    except Exception as e:
        return f"命令执行异常: {type(e).__name__}: {e}"


def check_process(name: str) -> str:
    """
    查询正在运行的进程（只读，不修改任何进程）。

    Args:
        name: 进程名称或关键词，用于 pgrep -af 匹配

    Returns:
        匹配到的进程列表，或"未找到"提示
    """
    try:
        result = subprocess.run(
            ["pgrep", "-af", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = [
            line
            for line in result.stdout.strip().splitlines()
            if "pgrep" not in line  # 排除 pgrep 自身
        ]
        if lines:
            return f"找到 {len(lines)} 个匹配进程:\n" + "\n".join(lines)
        else:
            return f"未找到匹配 '{name}' 的运行中进程"
    except Exception as e:
        return f"进程查询异常: {type(e).__name__}: {e}"
