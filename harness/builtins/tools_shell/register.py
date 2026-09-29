"""Shell 工具插件：权限门（permissions.shell = ask / allow / deny）。

权限判定统一走 :func:`harness.config.permission_mode`（非法值一律收敛为 ask），
本文件**只判断 "allow" 才放行**，避免再次出现 fail-open（缺陷审计 H-01）。

能力：
- ``cwd``：可在工作区**子目录**里执行（此前只能在工作区根，模型得自己 ``cd &&``，
  而 ``cd`` 一旦拼错就是静默跑错目录）。
- ``env``：附加环境变量（不继承修改全局环境）。
- ``max_output``：输出上限可调。
- 长命令边跑边把输出推给界面（``emit_agent_event``，kind=``tool_output``）。
  注意这**不是**给模型看的流式 —— 模型最终需要完整结果；真正的流式体验在
  对话层（``Agent.run_stream``）。当前桌面端未处理该 kind，未处理即静默忽略，
  因此是安全的增量能力。

审批模型（shell=ask 时按此顺序判定，前一步命中就不再问下一步）：

1. **白名单** —— ``permissions.shell_allow``（glob，如 ``"git status"`` /
   ``"git log*"``）。常用只读命令一次配好，不必每次都点确认。
2. **会话记忆** —— 本进程内用户点过「允许」的**同一条**命令不再重复询问。
   只记精确命令（折叠空白后），不做前缀推断：``git status`` 获批不等于
   ``git status --porcelain && rm -rf build`` 获批。
3. **询问用户** —— 走 ``host.confirm``；没有确认通道（非交互环境）时 **fail-closed**。

``shell=deny`` 优先级最高，白名单与记忆都不能绕过它。

**审批审计**：每一次放行/拒绝都追加一行 JSONL 到 ``<profile>/audit/shell.jsonl``
（``permissions.audit=false`` 可关）。审批是有副作用的决定，事后必须能回答
「这条命令是谁、什么时候、依据什么放行的」。审计写失败只告警，不改变命令去留。

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from harness.approvals import remember_shell_allow, request_approval
from harness.config import (
    audit_enabled,
    collapse_whitespace,
    matches_allowlist,
    permission_mode,
    shell_allowlist,
)
from harness.procutil import CREATE_NO_WINDOW
from harness.workspace import workspace_root

DEFAULT_TIMEOUT = 60
DEFAULT_MAX_OUTPUT = 4000

# 审计是「多线程里追加同一个文件」，用一把模块级锁串起来。
# （同一进程里可能同时挂载多个 profile 的 harness，各自拿各自的路径，互不干扰。）
_AUDIT_LOCK = threading.Lock()


def _as_int(value, default: int) -> int:
    """模型可能按错误的 schema 传字符串；这里做一次宽容转换。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _warn(message: str) -> None:
    try:
        print(f"[harness.tools_shell] 警告: {message}", file=sys.stderr)
    except Exception:  # noqa: BLE001 —— GUI 环境下 stderr 可能不可用
        pass


def register(ctx) -> None:
    host = ctx.host
    # 会话记忆：本进程内用户已批准过的命令（折叠空白后的精确文本）
    approved: set[str] = set()

    def _audit_path() -> Path:
        return Path(host.profile.root) / "audit" / "shell.jsonl"

    def _audit(config, command: str, decision: str, source: str, mode: str, cwd: str) -> None:
        """追加一条审批记录；失败只告警，绝不影响命令本身的去留。"""
        if not audit_enabled(config):
            return
        record = {
            "ts": time.time(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "command": command,
            "decision": decision,   # allow / deny
            "source": source,       # mode-allow / allowlist:xxx / session-approval / user-approved / user-denied / mode-deny / no-confirmer
            "mode": mode,
            "cwd": cwd or "",
        }
        path = _audit_path()
        try:
            with _AUDIT_LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            _warn(f"审计日志写入失败（{exc}）")

    def _authorize(command: str, cwd: str) -> str | None:
        """权限门：放行返回 None，否则返回一句给模型看的拒绝原因。"""
        config = host.profile.load_config()
        mode = permission_mode(config, "shell")

        if mode == "deny":  # deny 最高优先：白名单与记忆都不能绕过
            _audit(config, command, "deny", "mode-deny", mode, cwd)
            return "错误：shell 命令被权限配置拒绝（permissions.shell=deny）"
        if mode == "allow":
            _audit(config, command, "allow", "mode-allow", mode, cwd)
            return None

        text = collapse_whitespace(command)
        if text in approved:
            _audit(config, command, "allow", "session-approval", mode, cwd)
            return None
        hit = matches_allowlist(text, shell_allowlist(config))
        if hit:
            _audit(config, command, "allow", f"allowlist:{hit}", mode, cwd)
            return None

        has_channel = (callable(getattr(host, "request_approval", None))
                       or callable(getattr(host, "confirm", None)))
        if not has_channel:
            # 非交互环境没有确认通道 —— 不能默认放行（审计单列，便于排查）
            _audit(config, command, "deny", "no-confirmer", mode, cwd)
            return "错误：命令未获人工确认，已拒绝执行"
        decision = request_approval(host, {
            "kind": "shell",
            "title": "执行命令",
            "command": command,
            "cwd": str(cwd or ""),
        })
        if decision == "deny":
            _audit(config, command, "deny", "user-denied", mode, cwd)
            return "错误：命令未获人工确认，已拒绝执行"
        if decision == "allow_always":
            remember_shell_allow(host, command)
            approved.add(text)
            _audit(config, command, "allow", "user-always", mode, cwd)
            return None
        approved.add(text)
        _audit(config, command, "allow", "user-approved", mode, cwd)
        return None

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
        # 权限先于一切副作用（包括 cwd 解析）判定
        denied = _authorize(command, cwd)
        if denied is not None:
            return denied

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
                creationflags=CREATE_NO_WINDOW,
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

    def clear_approved() -> int:
        """清空会话记忆，返回被清掉的条数。"""
        count = len(approved)
        approved.clear()
        return count

    def command_approvals(args: str) -> str:
        arg = str(args or "").strip().lower()
        if arg in ("clear", "reset"):
            return f"已清空本会话记住的 {clear_approved()} 条已批准命令"
        config = host.profile.load_config()
        lines = [(
            f"permissions.shell = {permission_mode(config, 'shell')}"
            f"（audit={'on' if audit_enabled(config) else 'off'}）"
        )]

        patterns = shell_allowlist(config)
        lines.append(f"白名单 permissions.shell_allow（{len(patterns)} 条）:")
        if patterns:
            lines.extend(f"  ✓ {p}" for p in patterns)
        else:
            lines.append("  （空 —— 所有命令都需要确认）")

        lines.append(f"本会话已记住的已批准命令（{len(approved)} 条）:")
        if approved:
            lines.extend(f"  · {cmd}" for cmd in sorted(approved))
        else:
            lines.append("  （空）")

        path = _audit_path()
        lines.append(f"审计日志: {path}" + ("" if path.is_file() else "（尚无记录）"))
        lines.append("用 `/approvals clear` 清空本会话记忆")
        return "\n".join(lines)

    ctx.tools.register(run_command)
    ctx.commands.register("approvals", command_approvals, "查看/清空 shell 审批白名单与会话记忆")
    ctx.provide("shell_permissions", {
        "approved": lambda: sorted(approved),
        "clear": clear_approved,
        "allowlist": lambda: shell_allowlist(host.profile.load_config()),
        "audit_path": _audit_path,
    })
