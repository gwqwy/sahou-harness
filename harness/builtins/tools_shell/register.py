"""Shell 工具插件：权限门（permissions.shell = ask / allow / deny）。

权限判定统一走 :func:`harness.config.permission_mode`（非法值一律收敛为 ask），
本文件**只判断 "allow" 才放行**，避免再次出现 fail-open（缺陷审计 H-01）。

本轮补强：
- ``cwd``：可在工作区**子目录**里执行（此前只能在工作区根，模型得自己 ``cd &&``，
  而 ``cd`` 一旦拼错就是静默跑错目录）。
- ``env``：附加环境变量（不继承修改全局环境）。
- ``max_output``：输出上限可调。
- 长命令边跑边把输出推给界面（``emit_agent_event``，kind=``tool_output``）。
  注意这**不是**给模型看的流式 —— 模型最终需要完整结果；真正的流式体验在
  对话层（``Agent.run_stream``）。当前桌面端未处理该 kind，未处理即静默忽略，
  因此是安全的增量能力。

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
"""

from __future__ import annotations

import os
import subprocess
import threading

from harness.config import permission_mode
from harness.workspace import workspace_root

DEFAULT_TIMEOUT = 60
DEFAULT_MAX_OUTPUT = 4000


def _as_int(value, default: int) -> int:
    """模型可能按错误的 schema 传字符串；这里做一次宽容转换。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def register(ctx) -> None:
    host = ctx.host

    def _resolve_cwd(cwd: str) -> str:
        """把 cwd 解析成工作区内的绝对目录；越界即抛 ValueError。"""
        workspace = workspace_root(host)
        if not str(cwd or "").strip():
            return str(workspace)
        candidate = (workspace / str(cwd).strip()).resolve()
        if candidate != workspace and workspace not in candidate.parents:
            raise ValueError(f"cwd 越出工作区: {cwd}（工作区: {workspace}）")
        if not candidate.is_dir():
            raise ValueError(f"cwd 不是目录: {cwd}")
        return str(candidate)

    def run_command(command: str, timeout: int = DEFAULT_TIMEOUT, cwd: str = "",
                    env: dict | None = None, max_output: int = DEFAULT_MAX_OUTPUT) -> str:
        """在工作区执行 shell 命令并返回输出（受权限门控制）。

        Args:
            command: 完整命令行
            timeout: 超时秒数
            cwd: 工作区内的相对目录，命令在该目录执行（默认工作区根）
            env: 附加环境变量，如 {"PYTHONIOENCODING": "utf-8"}
            max_output: 回传输出的字符上限
        """
        mode = permission_mode(host.profile.load_config(), "shell")
        if mode == "deny":
            return "错误：shell 命令被权限配置拒绝（permissions.shell=deny）"
        if mode != "allow":  # 只有显式 allow 才直接放行；其余（含非法值收敛来的 ask）一律确认
            confirm = getattr(host, "confirm", None)
            if confirm is None or not confirm(f"执行命令: {command}"):
                return "错误：命令未获人工确认，已拒绝执行"

        seconds = max(1, _as_int(timeout, DEFAULT_TIMEOUT))
        cap = max(1, _as_int(max_output, DEFAULT_MAX_OUTPUT))
        try:
            workdir = _resolve_cwd(cwd)
        except ValueError as exc:
            return f"错误：{exc}"

        child_env = None
        if isinstance(env, dict) and env:
            child_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}}

        try:
            process = subprocess.Popen(
                command, shell=True, cwd=workdir, env=child_env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            return f"错误：找不到命令: {command}"

        chunks: list[str] = []
        emit = getattr(host, "emit_agent_event", None)

        def pump() -> None:
            # 合并 stdout/stderr 按行读取：日志类命令的时序才不会被拆乱
            for line in process.stdout or []:
                chunks.append(line)
                if callable(emit):
                    try:
                        emit({"kind": "tool_output", "tool": "run_command", "text": line[:2000]})
                    except Exception:  # noqa: BLE001 —— 推送失败不影响命令执行
                        pass

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        reader.join(seconds)
        timed_out = reader.is_alive()
        if timed_out:
            process.kill()
            reader.join(5)
        # 管道读到 EOF 不会自动回填 returncode（只有 wait/poll/communicate 会），
        # 少了这一步拿到的永远是 None。
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            timed_out = True
        if process.stdout is not None:
            process.stdout.close()

        output = "".join(chunks).strip()
        prefix = f"[超时 >{seconds}s] " if timed_out else f"[exit {process.returncode}] "
        if not output:
            output = "（无输出）"
        if len(output) > cap:
            return prefix + output[:cap] + f"\n...（共 {len(output)} 字符，已截断）"
        return prefix + output

    ctx.tools.register(run_command)
