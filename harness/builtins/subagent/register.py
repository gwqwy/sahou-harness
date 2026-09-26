"""子 agent 插件：给主 agent 一个「派活」工具。

为什么有用：长任务里最容易把主对话的上下文撑爆的就是「翻一堆文件找答案」这类
子任务。派给一个**上下文隔离**的子 agent，主对话只拿回结论 —— 这是
nanoagent ``multi`` 模块（``agent_as_tool`` / ``Team`` / ``Pipeline``）的核心思路，
这里取最常用的一档：主 agent 按需派生一个一次性子 agent。

设计取舍：
- 子 agent 复用主 agent 的模型与工具集，但**剔除 spawn_subagent**（否则会无限递归）
  与 switch_model（子任务不该改全局模型选择）。
- 每次调用一个全新会话 id ⇒ 子 agent 不累积历史，行为可预期，也不会越跑越贵。
- 子 agent 的构建是**惰性**的：注册时模型/工具可能还没就绪（激活顺序、未配置模型），
  所以放到第一次调用时才建。

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
"""

from __future__ import annotations

import uuid

SUB_INSTRUCTIONS = (
    "你是一个专注于单一子任务的助手。直接动手完成它，然后只汇报结论："
    "做了什么、产物在哪里、结论是什么。不要寒暄，不要复述任务。"
)

EXCLUDED_TOOLS = {"spawn_subagent", "switch_model"}


def register(ctx) -> None:
    host = ctx.host
    state: dict = {"agent": None}

    def _sub_agent():
        if state["agent"] is None:
            factory = host.service("agent_factory")
            parent = factory() if callable(factory) else None
            if parent is None or getattr(parent, "llm", None) is None:
                raise RuntimeError("主 agent 尚未就绪（先配置模型）")
            from nanoagent import Agent

            tools = [t for t in host.collect_tools() if t.name not in EXCLUDED_TOOLS]
            state["agent"] = Agent(name="子助手", instructions=SUB_INSTRUCTIONS,
                                   llm=parent.llm, tools=tools, max_iterations=8)
        return state["agent"]

    def spawn_subagent(task: str = "") -> str:
        """把一个独立子任务交给上下文隔离的子 agent 完成，只返回它的结论。

        适合「查资料 / 翻文件找答案 / 跑一段独立验证」这类会消耗大量上下文的工作；
        不适合需要与用户来回确认的任务。

        Args:
            task: 子任务描述（必填），要写清目标与验收标准（子 agent 看不到当前对话）
        """
        # 默认空串而不是「必填位置参数」：模型偶尔会漏传，此时应回一句中文提示，
        # 而不是让 TypeError 冒到工具层变成一句英文内部报错。
        text = str(task or "").strip()
        if not text:
            return "错误：task 不能为空"
        try:
            agent = _sub_agent()
        except Exception as exc:  # noqa: BLE001 —— 作为工具结果回给主模型，而不是炸掉整轮
            return f"错误：子 agent 不可用（{type(exc).__name__}: {exc}）"
        session_id = f"sub-{uuid.uuid4().hex[:8]}"
        try:
            result = agent.run(text, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            return f"错误：子任务执行失败（{type(exc).__name__}: {exc}）"
        output = (result.content or "").strip()
        return output or "（子 agent 没有给出结论）"

    ctx.tools.register(spawn_subagent)

    def factory():
        """供其它插件/界面复用的子 agent 工厂（惰性）。"""
        return _sub_agent()

    ctx.provide("subagent_factory", factory)
