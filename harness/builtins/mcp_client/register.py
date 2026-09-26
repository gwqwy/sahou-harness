"""MCP 客户端插件：stdio / SSE / HTTP 三种传输，工具直接并入 agent 工具集。

连接生命周期跑在**专用后台线程的常驻事件循环**上：MCP session 绑定创建它的
loop，工具调用也必须回到同一个 loop（run_coroutine_threadsafe），否则触发
「attached to a different loop」。工具包装成**同步闭包**注册给 agent——
chat_loop 的 ask 是同步 API，包装后 is_async 恒为 False，不会再走进需要
AsyncLLM 的 arun 路径（H-02：配了 MCP 对话必抛 TypeError）。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

_HANDSHAKE_TIMEOUT = 120.0  # 连接/取工具列表上限（秒）
_CALL_TIMEOUT = 300.0       # 单次工具调用上限（MCP 侧可能跑长任务）
_DISCONNECT_TIMEOUT = 15.0


def register(ctx) -> None:
    host = ctx.host
    config = host.profile.load_config()
    servers = config.get("mcpServers") or {}
    if not servers:
        return

    # 常驻 loop + 专职线程：所有 MCP 协程都投递到这里执行，session 与 loop 永不分离。
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="harness-mcp", daemon=True)
    thread.start()
    connections: list[Any] = []

    def run(coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    def shutdown_loop() -> None:
        try:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
        except Exception:  # noqa: BLE001 —— 关闭失败不该阻断其余清理
            pass
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass

    def disconnect_all() -> None:
        # 逐层容错：一个 server 断不开不能挡住后面的（N-03 同源教训）
        for server in connections:
            try:
                run(server.disconnect(), _DISCONNECT_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
        connections.clear()
        shutdown_loop()

    def wrap_sync(tool):
        """把异步 Tool 包装成同 schema 的同步 Tool：调用时投递回专用 loop。"""
        from nanoagent.tools import Tool

        def call(**arguments):
            # 注意签名必须是 **kwargs：Tool.invoke 以 func(**arguments) 调用
            return run(tool.arun(arguments), _CALL_TIMEOUT)

        call.__name__ = tool.name
        call.__doc__ = tool.description
        return Tool(name=tool.name, description=tool.description,
                    parameters=tool.parameters, func=call, thread_safe=False)

    def connect_all():
        from nanoagent.mcp import MCPServer

        tools = []
        for name, cfg in servers.items():
            try:
                if cfg.get("url"):
                    transport = (cfg.get("type") or "http").lower()
                    builder = MCPServer.connect_sse if transport == "sse" else MCPServer.connect_http
                    server = run(builder(cfg["url"], cfg.get("headers")), _HANDSHAKE_TIMEOUT)
                else:
                    server = run(MCPServer.connect_stdio(
                        cfg["command"], cfg.get("args") or [], cfg.get("env")), _HANDSHAKE_TIMEOUT)
                connections.append(server)
                tools.extend(run(server.tools(), _HANDSHAKE_TIMEOUT))
            except Exception as exc:  # noqa: BLE001 —— 单个服务器失败不阻断
                ctx.skipped.append(f"mcp:{name}: {type(exc).__name__}: {exc}")
        for tool in tools:
            ctx.tools.register(wrap_sync(tool))

        def dispose() -> None:
            disconnect_all()

        return dispose

    ctx.effect(connect_all)
