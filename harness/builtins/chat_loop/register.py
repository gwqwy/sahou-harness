"""对话循环插件：懒构建 nanoagent Agent（等所有插件激活后再聚合工具）。"""

from __future__ import annotations

import json
import time
from typing import Any

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


def _build_memory(cfg: dict, llm):
    """按 config.json 的 ``memory`` 段构建记忆（缺省=全量 Memory，行为与既往一致）。

    - ``{"type": "summary", "max_tokens": 24000}``：SummaryMemory——历史超窗时
      自动把最旧消息压缩成摘要（用当前对话的 LLM），只保留摘要 + 最近几条。
      长会话的 token 成本从线性增长变为近似封顶。
    - ``{"type": "sliding", "max_messages": 40}``：滑动窗口，超限丢最旧。
    - 缺省 / 其他取值：全量 Memory（不裁剪）。
    """
    from nanoagent.memory import Memory, SummaryMemory

    mtype = str(cfg.get("type") or "full").lower()
    if mtype == "summary":
        return SummaryMemory(
            max_tokens=int(cfg.get("max_tokens") or 24000),
            keep_recent=int(cfg.get("keep_recent") or 6),
            llm=llm,
        )
    if mtype == "sliding":
        return Memory(max_messages=int(cfg.get("max_messages") or 40))
    return Memory()


def _record_usage(host, session_id: str, model: str, usage: dict) -> None:
    """每轮对话追加一条用量记录到 <profile>/usage.jsonl（`sha usage` 的数据源）。"""
    rec = {"ts": time.time(), "session": session_id, "model": model,
           "prompt_tokens": int((usage or {}).get("prompt_tokens") or 0),
           "completion_tokens": int((usage or {}).get("completion_tokens") or 0)}
    try:
        with open(host.profile.root / "usage.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 —— 统计失败不影响对话
        pass


def register(ctx) -> None:
    host = ctx.host
    state: dict[str, Any] = {"agent": None, "session_id": "default"}

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
            memory = _build_memory(host.profile.load_config().get("memory") or {}, llm)
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

    def _stream_events(agent, message: str, session_id: str, images: list | None = None):
        """同步流式：直接透传 ``run_stream`` 的事件（delta / tool_call / done）。

        工具是同步执行的（MCP 已包装为同步闭包，其余协程工具由 nanoagent
        单独起循环跑完），因此不存在「异步工具要走 arun_stream」的分支——
        那条路径需要 AsyncLLM，而 models 只构建同步 LLM（H-02 的流式面）。
        """
        yield from agent.run_stream(message, session_id=session_id, images=images)

    def _bind_session(session_id: str | None) -> str:
        """确定本轮会话；跨会话时必须丢掉 agent，否则记忆里仍是上一个会话的历史。"""
        if session_id is None:
            return state["session_id"]
        if session_id != state["session_id"]:
            state["agent"] = None
            state["session_id"] = session_id
        return session_id

    def _validate_images(images):
        """多模态输入校验（功能7）：本地路径须落在工作区路径监狱内，URL 原样放行。

        校验失败抛 ValueError（界面已把异常文本直接展示给用户，可读即可）。
        """
        from harness.workspace import safe_images

        try:
            return safe_images(host, images)
        except OSError as exc:  # FileNotFoundError 等 → 统一成可读 ValueError
            raise ValueError(f"图片输入无效: {exc}") from exc

    def ask(message: str, session_id: str | None = None, images: list | None = None) -> dict:
        """执行一轮对话，并持久化会话。

        统一走同步 :meth:`agent.run`：MCP 工具已由 mcp_client 包装成同步闭包
        （内部投递回专用 loop），其余协程工具由 nanoagent 的同步执行路径
        单独起事件循环跑完 —— 都不需要 AsyncLLM。此前这里检测到异步工具就
        切 ``arun``，而 models 只构建同步 LLM，导致配了 MCP 对话必抛
        TypeError（H-02）。

        Args:
            message: 用户输入
            session_id: 目标会话；省略则沿用当前会话（由 new_session 维护）
            images: 可选图片源列表（http(s)/data URL 或工作区内本地路径），
                非空时本轮消息升级为多模态（模型需支持 vision）
        """
        # 会话切换必须重建 agent：否则记忆里仍是上一个会话的历史（跨会话串味）
        session_id = _bind_session(session_id)
        agent = get_agent()
        result = agent.run(message, session_id=session_id, images=_validate_images(images))
        host.profile.save_session(session_id, agent.memory.history(session_id))
        runtime = host.service("models_runtime") or {}
        _record_usage(host, session_id, runtime.get("current", ""), result.usage)
        return {"reply": result.content, "reasoning": getattr(result, "reasoning", ""),
                "tool_calls": result.tool_calls,
                "model": runtime.get("current", ""), "usage": result.usage}

    def ask_stream(message: str, session_id: str | None = None, images: list | None = None):
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
        yield from _stream_events(agent, message, session_id, _validate_images(images))
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
