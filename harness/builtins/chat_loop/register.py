"""对话循环插件：懒构建 nanoagent Agent（等所有插件激活后再聚合工具）。"""

from __future__ import annotations

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

    def ask(message: str, session_id: str = "default") -> dict:
        """执行一轮对话（自动适配同步/异步工具），并持久化会话。"""
        state["session_id"] = session_id  # 先定会话：懒构建时按它回放历史
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

    def new_session() -> str:
        state["agent"] = None  # 下次 ask 重建（记忆随之清空）
        return "已开始新会话。"

    ctx.provide("agent_factory", get_agent)
    ctx.provide("ask", ask)
    ctx.provide("new_session", new_session)
