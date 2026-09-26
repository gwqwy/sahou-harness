"""Shell 工具插件：权限门（permissions.shell = ask / allow / deny）。

权限判定统一走 :func:`harness.config.permission_mode`（非法值一律收敛为 ask），
本文件**只判断 "allow" 才放行**，避免再次出现 fail-open（缺陷审计 H-01）。
"""

from __future__ import annotations

import subprocess

# 注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
from harness.config import permission_mode

DEFAULT_TIMEOUT = 60


def register(ctx) -> None:
    host = ctx.host

    def _as_int(value, default: int) -> int:
        """模型可能按错误的 schema 传字符串；这里做一次宽容转换。"""
        if isinstance(value, bool) or value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def run_command(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        """在工作区执行 shell 命令并返回输出（受权限门控制）。

        Args:
            command: 完整命令行
            timeout: 超时秒数
        """
        mode = permission_mode(host.profile.load_config(), "shell")
        if mode == "deny":
            return "错误：shell 命令被权限配置拒绝（permissions.shell=deny）"
        if mode != "allow":  # 只有显式 allow 才直接放行；其余（含非法值收敛来的 ask）一律确认
            confirm = getattr(host, "confirm", None)
            if confirm is None or not confirm(f"执行命令: {command}"):
                return "错误：命令未获人工确认，已拒绝执行"
        seconds = max(1, _as_int(timeout, DEFAULT_TIMEOUT))
        try:
            completed = subprocess.run(
                command, shell=True, cwd=str(host.workspace),
                capture_output=True, text=True, timeout=seconds,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            return f"错误：找不到命令: {command}"
        except subprocess.TimeoutExpired:
            return f"错误：命令超时（>{seconds}s）"
        output = (completed.stdout or "") + (completed.stderr or "")
        out = output.strip() or "（无输出）"
        return f"[exit {completed.returncode}] " + (out[:4000] + f"\n...（共 {len(out)} 字符，已截断）" if len(out) > 4000 else out)

    ctx.tools.register(run_command)
