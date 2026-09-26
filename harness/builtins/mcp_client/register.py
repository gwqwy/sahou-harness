"""MCP 客户端插件：stdio / SSE / HTTP 三种传输，工具直接并入 agent 工具集。

连接在 ctx.effect 中建立，disposer 负责断开——卸载插件即自动回收连接。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


def register(ctx) -> None:
    host = ctx.host
    config = host.profile.load_config()
    servers = config.get("mcpServers") or {}
    if not servers:
        return

    connections = []  # 已建立的 MCPServer 连接

    # 整个连接/断开生命周期共用一个事件循环：MCP session 对象绑定在创建它的 loop 上，
    # 若每步都 asyncio.run（各自新建+关闭 loop），跨 loop 复用会触发
    # 「attached to a different loop」（P2 #20）。
    loop = asyncio.new_event_loop()

    def run(coro):
        return loop.run_until_complete(coro)

    def disconnect_all() -> None:
        for server in connections:
            try:
                run(server.disconnect())
            except Exception:  # noqa: BLE001
                pass
        connections.clear()
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass

    def connect_all() -> Callable:
        from nanoagent.mcp import MCPServer

        tools = []
        for name, cfg in servers.items():
            try:
                if cfg.get("url"):
                    transport = (cfg.get("type") or "http").lower()
                    builder = MCPServer.connect_sse if transport == "sse" else MCPServer.connect_http
                    server = run(builder(cfg["url"], cfg.get("headers")))
                else:
                    server = run(MCPServer.connect_stdio(
                        cfg["command"], cfg.get("args") or [], cfg.get("env")))
                connections.append(server)
                tools.extend(run(server.tools()))
            except Exception as exc:  # noqa: BLE001 —— 单个服务器失败不阻断
                ctx.skipped.append(f"mcp:{name}: {type(exc).__name__}: {exc}")
        for tool in tools:
            ctx.tools.register(tool)

        def dispose() -> None:
            disconnect_all()

        return dispose

    ctx.effect(connect_all)
