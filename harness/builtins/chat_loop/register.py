"""对话循环插件：懒构建 nanoagent Agent（等所有插件激活后再聚合工具）。"""

from __future__ import annotations

import time

INSTRUCTIONS = (
    "你是卅 harness 的中文编程助手，简洁、诚实、善用工具。"
    "无论用户用什么语言提问，回复必须始终使用简体中文："
    "解释、结论、列表、代码注释全部用中文，代码标识符和必要的英文术语可以保留原文。"
)


class _EventTracer:
    """包装 nanoagent tracer：把思考/工具步骤实时推送给桌面端界面。"""

    def __init__(self, inner, host) -> None:
        self._inner = inner
        self._host = host

    def start_run(self, *args, **kwargs):
        return self._inner.start_run(*args, **kwargs)

    def end_run(self, *args, **kwargs):
        return self._inner.end_run(*args, **kwargs)

    def log(self, kind, **kwargs):
        self._inner.log(kind, **kwargs)
        emit = getattr(self._host, "emit_agent_event", None)
        if not callable(emit):
            return
        try:
            if kind == "llm_call":
                emit({
                    "kind": "llm",
                    "reasoning": str(kwargs.get("reasoning") or "")[:4000],
                    "tools": [name for name, _ in (kwargs.get("tool_calls") or [])],
                })
            elif kind == "tool_call":
                emit({
                    "kind": "tool",
                    "tool": str(kwargs.get("tool") or ""),
                    "elapsed_ms": kwargs.get("elapsed_ms") or 0,
                    "result": str(kwargs.get("result") or "")[:300],
                })
        except Exception:  # noqa: BLE001 —— 事件推送失败不影响对话
            pass


def register(ctx) -> None:
    host = ctx.host
    state = {"agent": None, "session_id": "default"}

    def _new_session_id() -> str:
        """生成一个尚未被占用的会话 id（时间戳 + 递增序号兜底）。

        会话 id 会直接作为 ``sessions/<id>.json`` 的文件名，因此不能用冒号等
        Windows 非法字符。
        """
        base = time.strftime("s-%Y%m%d-%H%M%S")
        try:
            existing = set(host.profile.session_ids())
        except OSError:
            existing = set()
        if base not in existing:
            return base
        counter = 2
        while f"{base}-{counter}" in existing:
            counter += 1
        return f"{base}-{counter}"

    def get_agent():
        if state["agent"] is None:
            from pathlib import Path

            from nanoagent import Agent
            from nanoagent.memory import Memory
            from nanoagent.skills import SkillRegistry

            runtime = host.service("models_runtime") or {}
            pool = runtime.get("pool") or {}
            if not pool:
                raise RuntimeError("没有可用模型：请在 .harness/profiles/<名>/config.json 的 models 里配置")
            llm = pool[runtime["current"]]

            registry = SkillRegistry()
            for skill_dir in host.collect_skill_dirs():
                registry.add_dir(skill_dir)
            profile_skills = Path(host.profile.root) / "skills"  # 用户安装的技能
            if profile_skills.is_dir():
                registry.add_dir(profile_skills)

            instructions = INSTRUCTIONS
            memory = Memory()
            session_id = state["session_id"]
            for item in host.profile.load_session(session_id):
                if item.get("role") in ("user", "assistant"):
                    memory.add(session_id, item["role"], item.get("content", ""))

            agent = Agent(name="卅助手", instructions=instructions, llm=llm,
                          tools=host.collect_tools(), memory=memory)
            agent.tracer = _EventTracer(agent.tracer, host)
            if len(registry):
                agent.enable_skills(registry)
            state["agent"] = agent
        return state["agent"]

    def ask(message: str, session_id: str | None = None) -> dict:
        """执行一轮对话（自动适配同步/异步工具），并持久化会话。

        Args:
            message: 用户输入
            session_id: 目标会话；省略则沿用当前会话（由 new_session 维护）
        """
        # 会话切换必须重建 agent：否则记忆里仍是上一个会话的历史（跨会话串味）
        if session_id is None:
            session_id = state["session_id"]
        elif session_id != state["session_id"]:
            state["agent"] = None
            state["session_id"] = session_id
        agent = get_agent()
        has_async = any((agent.tools.get(n) and agent.tools.get(n).is_async) for n in agent.tools.names())
        if has_async:
            import asyncio

            result = asyncio.run(agent.arun(message, session_id=session_id))
        else:
            result = agent.run(message, session_id=session_id)
        host.profile.save_session(session_id, agent.memory.history(session_id))
        runtime = host.service("models_runtime") or {}
        return {"reply": result.content, "reasoning": getattr(result, "reasoning", ""),
                "tool_calls": result.tool_calls,
                "model": runtime.get("current", ""), "usage": result.usage}

    def new_session(session_id: str | None = None) -> str:
        """开始一个新会话，返回新的会话 id（旧会话历史仍保留在磁盘上）。

        必须**同时**换 id 与清 agent。旧实现只把 agent 置空、id 保持不变，
        于是下一次 ask 又按同一个 id 从磁盘把旧历史回放回来 ——
        `/new` 看起来说了「已开始新会话」，实际一条都没清掉。
        """
        state["session_id"] = session_id or _new_session_id()
        state["agent"] = None
        return state["session_id"]

    def current_session() -> str:
        """当前会话 id（供界面显示 / 保存）。"""
        return state["session_id"]

    ctx.provide("agent_factory", get_agent)
    ctx.provide("ask", ask)
    ctx.provide("new_session", new_session)
    # 同一实现、更贴调用点的名字：明确「切到某个已有会话」时用它
    ctx.provide("switch_session", new_session)
    ctx.provide("current_session", current_session)
