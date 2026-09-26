"""追踪插件：把每轮对话的 trace 落盘。

此前只有一个内存里的 :class:`_EventTracer`，且把每条事件截断到 4000/300 字符后
推给界面 —— 关掉窗口就什么都不剩，出问题无法复盘。nanoagent 本来就有
:class:`~nanoagent.tracing.Tracer`（JSONL 落盘）与
:mod:`nanoagent.observability`（摘要 / 报告 / OTel 导出），缺的只是接上。

配置（config.json，可选）：

    "tracing": {"enabled": true, "dir": "traces"}

- ``enabled`` 显式设为 false 可关闭（默认开启：出问题时才想得起开就太晚了）
- ``dir`` 相对 profile 目录，默认 ``traces``
- 文件名按天切分（``2026-09-26.jsonl``），Tracer 是**追加**写，不会互相截断

落盘内容是 JSONL，一行一个事件，可用 ``sha trace`` 看摘要。

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
"""

from __future__ import annotations

import time
from pathlib import Path


def register(ctx) -> None:
    host = ctx.host
    settings = host.profile.load_config().get("tracing")
    settings = settings if isinstance(settings, dict) else {}

    if settings.get("enabled") is False:
        ctx.skipped.append("disabled: tracing.enabled=false")
        return

    from nanoagent.tracing import Tracer

    out_dir = Path(host.profile.root) / str(settings.get("dir") or "traces")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        ctx.skipped.append(f"cannot-create-dir: {exc}")
        return

    def new_tracer() -> Tracer:
        """给新建的 agent 一个按天归档的落盘 tracer。"""
        return Tracer(path=out_dir / f"{time.strftime('%Y-%m-%d')}.jsonl")

    ctx.provide("tracing", {"new_tracer": new_tracer, "dir": out_dir})

    def command_tracing(args: str) -> str:
        files = sorted(out_dir.glob("*.jsonl"))
        if not files:
            return f"暂无 trace（目录: {out_dir}）"
        lines = [f"trace 目录: {out_dir}"]
        for path in files[-7:]:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            lines.append(f"  {path.name}  {size} 字节")
        lines.append("  用 `sha trace` 看最新一份的摘要")
        return "\n".join(lines)

    ctx.commands.register("tracing", command_tracing, "查看 trace 落盘情况")
