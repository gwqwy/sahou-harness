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

    def log(self, event_type, **data):
        # 首个参数必须叫 event_type（不是 kind）：nanoagent 的 Tracer.log 签名是
        # log(event_type, **data)，而 agent.py 会传 kind="input"/"output" 作为数据，
        # 若此处命名成 kind 就会 "got multiple values for argument 'kind'"。
        self._inner.log(event_type, **data)
        emit = getattr(self._host, "emit_agent_event", None)
        if not callable(emit):
            return
        try:
            if event_type == "llm_call":
                emit({
                    "kind": "llm",
                    "reasoning": str(data.get("reasoning") or "")[:4000],
                    "tools": [name for name, _ in (data.get("tool_calls") or [])],
                })
            elif event_type == "tool_call":
                emit({
                    "kind": "tool",
                    "tool": str(data.get("tool") or ""),
                    "elapsed_ms": data.get("elapsed_ms") or 0,
                    "result": str(data.get("result") or "")[:300],
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

            # 护栏与 tracer 都由插件提供，缺席即不启用（软依赖，不写进 inject）
            rules = host.service("guardrails") or {}
            make_tracer = (host.service("tracing") or {}).get("new_tracer")

            agent = Agent(name="卅助手", instructions=instructions, llm=llm,
                          tools=host.collect_tools(), memory=memory,
                          input_guardrails=rules.get("input") or None,
                          output_guardrails=rules.get("output") or None,
                          tracer=make_tracer() if callable(make_tracer) else None)
            agent.tracer = _EventTracer(agent.tracer, host)
            if len(registry):
                agent.enable_skills(registry)
            state["agent"] = agent
        return state["agent"]

    def can_stream() -> bool:
        """当前模型是否支持流式（自建/自定义 LLM 可能只有 chat）。

        界面据此选择走流式还是一次性，**不做「先试再回退」** ——
        一旦流式跑到一半才失败，工具副作用可能已经发生了，重跑一遍是危险的。
        """
        try:
            llm = getattr(get_agent(), "llm", None)
        except Exception:  # noqa: BLE001 —— 模型未配置等
            return False
        return callable(getattr(llm, "chat_stream", None))

    def _stream_events(agent, message: str, session_id: str, has_async: bool):
        """把同步/异步两条流式接口统一成一个同步生成器。

        ``arun_stream`` 是异步生成器，同步语境下没法「边产出边消费」地 asyncio.run，
        所以用后台线程 + queue 桥接；异常与结束标记都通过队列回传再抛出。
        """
        if not has_async:
            yield from agent.run_stream(message, session_id=session_id)
            return
        import asyncio
        import queue
        import threading

        channel: queue.Queue = queue.Queue()
        finished = object()

        def worker() -> None:
            async def consume() -> None:
                async for event in agent.arun_stream(message, session_id=session_id):
                    channel.put(event)

            try:
                asyncio.run(consume())
            except BaseException as exc:  # noqa: BLE001 —— 交给消费者在同步侧抛出
                channel.put(exc)
            finally:
                channel.put(finished)

        threading.Thread(target=worker, name="harness-stream", daemon=True).start()
        while True:
            item = channel.get()
            if item is finished:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    def _bind_session(session_id: str | None) -> str:
        """确定本轮会话；跨会话时必须丢掉 agent，否则记忆里仍是上一个会话的历史。"""
        if session_id is None:
            return state["session_id"]
        if session_id != state["session_id"]:
            state["agent"] = None
            state["session_id"] = session_id
        return session_id

    def ask(message: str, session_id: str | None = None) -> dict:
        """执行一轮对话（自动适配同步/异步工具），并持久化会话。

        Args:
            message: 用户输入
            session_id: 目标会话；省略则沿用当前会话（由 new_session 维护）
        """
        # 会话切换必须重建 agent：否则记忆里仍是上一个会话的历史（跨会话串味）
        session_id = _bind_session(session_id)
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

    def ask_stream(message: str, session_id: str | None = None):
        """流式对话：依次产出 ``delta`` / ``tool_call`` / ``done`` 事件。

        ``done`` 事件里的 ``result`` 与 :func:`ask` 同源（完整 AgentResult）。

        会话落盘**由本函数在流结束时补上**：nanoagent 的 ``run_stream`` 只把回合
        记进内存里的 agent.memory，并不写 profile 的会话文件（那是 harness 的职责，
        ``ask`` 里显式调 ``save_session``）。此前这里漏了这步，于是流式跑完关掉
        界面，这一轮对话就凭空消失了。

        模型不支持 ``chat_stream`` 时会抛错 —— 调用方应先用 :func:`can_stream`
        判断，不要「先试再回退」（跑到一半失败会有重复的工具副作用）。
        """
        session_id = _bind_session(session_id)
        agent = get_agent()
        has_async = any((agent.tools.get(n) and agent.tools.get(n).is_async)
                        for n in agent.tools.names())
        for event in _stream_events(agent, message, session_id, has_async):
            yield event
        # 消费方提前 break 时生成器被关闭，这里不会执行（与 ask 中断即不落盘一致）
        host.profile.save_session(session_id, agent.memory.history(session_id))

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
    ctx.provide("ask_stream", ask_stream)
    ctx.provide("can_stream", can_stream)
    ctx.provide("new_session", new_session)
    # 同一实现、更贴调用点的名字：明确「切到某个已有会话」时用它
    ctx.provide("switch_session", new_session)
    ctx.provide("current_session", current_session)
