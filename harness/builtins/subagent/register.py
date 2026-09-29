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
    "如果某个工具被拒绝（如命令未获人工确认），不要反复重试同一个工具，"
    "改用其他可用工具，或直接说明这项限制——重试只会浪费时间。"
)

EXCLUDED_TOOLS = {"spawn_subagent", "switch_model"}

# 只读模式的额外排除（F7，串行优于并发）：子代理不允许旁路写代码库
READONLY_EXCLUDED = {"write_file", "edit_file", "run_command",
                     "git_add", "git_commit"}

# 非交互场景下必然被拒的工具：shell 权限不是 allow 时，子 agent 带上它们
# 只会拿到一串「未获人工确认」并在重试里空转（实测 17 秒 35 步全是空跑）
SHELL_TOOLS = {"run_command", "git_commit", "git_add"}


class _SubTracer:
    """子 agent 的轻量 tracer：跨轮累计工具名 + 把工具动作推给界面。

    缓存的子 agent 每轮复用同一个 tracer 会串数据，所以每次 spawn 新建一个。
    """

    def __init__(self, emit=None, sub_session: str = "") -> None:
        self.tools: list[str] = []
        self.trace: list[dict] = []   # [{name, result}]：结论为空时诊断/面板展示用
        self._emit = emit
        self._sid = sub_session

    def start_run(self, *args, **kwargs):
        return None

    def end_run(self, *args, **kwargs):
        return None

    def log(self, event_type, **data):
        if event_type != "tool_call":
            return
        name = str(data.get("tool") or "")
        result = str(data.get("result") or "")
        if name:
            self.tools.append(name)
            self.trace.append({"name": name, "result": result[:200]})
        if callable(self._emit):
            try:
                self._emit({"kind": "subagent", "phase": "tool", "tool": name,
                            "sub_session": self._sid,
                            "result": result[:160]})
            except Exception:  # noqa: BLE001 —— 推送失败不影响子任务
                pass


def register(ctx) -> None:
    host = ctx.host
    state: dict = {"agent": None}

    def _emit(payload: dict) -> None:
        """把子 agent 动态推给界面（桌面端通过 emit_agent_event 转发到 live 区）。"""
        emit = getattr(host, "emit_agent_event", None)
        if callable(emit):
            try:
                emit(payload)
            except Exception:  # noqa: BLE001 —— 推送失败不影响子任务
                pass

    def _record(entry: dict) -> None:
        """把一次子 agent 调用追加到 profile/subagents.jsonl（右侧面板数据源）。

        带 ``id``（uuid 短哈希）：桌面端「移除单条记录」按它定位；
        旧版记录没有 id，只能整表清空。
        """
        try:
            import json

            entry = {"id": uuid.uuid4().hex[:8], **entry}
            path = host.profile.root / "subagents.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 —— 记录失败不影响主流程
            pass

    def _sub_agent(tracer=None):
        if tracer is not None:
            # 带 tracer 的按次构建：缓存会串工具记录，结论质量比构建开销更重要
            return _build_agent(tracer)
        if state["agent"] is None:
            state["agent"] = _build_agent(None)
        return state["agent"]

    def _build_agent(tracer):
        factory = host.service("agent_factory")
        parent = factory() if callable(factory) else None
        if parent is None or getattr(parent, "llm", None) is None:
            raise RuntimeError("主 agent 尚未就绪（先配置模型）")
        from nanoagent import Agent

        config = host.profile.load_config()
        sub_cfg = config.get("subagent") if isinstance(config.get("subagent"), dict) else {}
        excluded = set(EXCLUDED_TOOLS)
        if sub_cfg.get("readonly"):
            excluded |= READONLY_EXCLUDED
        # shell 未放行时不带 shell 类工具：子 agent 无法人工确认，带了也是空转
        perms = config.get("permissions") if isinstance(config.get("permissions"), dict) else {}
        if str(perms.get("shell") or "").lower() != "allow":
            excluded |= SHELL_TOOLS
        tools = [t for t in host.collect_tools() if t.name not in excluded]
        return Agent(name="子助手", instructions=SUB_INSTRUCTIONS,
                     llm=parent.llm, tools=tools, max_iterations=8, tracer=tracer)

    def spawn_subagent(task: str = "") -> str:
        """把一个独立子任务交给上下文隔离的子 agent 完成，只返回它的结论。

        适合「查资料 / 翻文件找答案 / 跑一段独立验证」这类会消耗大量上下文的工作；
        不适合需要与用户来回确认的任务。

        Args:
            task: 子任务描述（必填），要写清目标与验收标准（子 agent 看不到当前对话）
        """
        # 默认空串而不是「必填位置参数」：模型偶尔会漏传，此时应回一句中文提示，
        # 而不是让 TypeError 冒到工具层变成一句英文内部报错。
        import time

        text = str(task or "").strip()
        if not text:
            return "错误：task 不能为空"
        session_id = f"sub-{uuid.uuid4().hex[:8]}"
        tracer = _SubTracer(_emit, session_id)
        try:
            agent = _sub_agent(tracer)
        except Exception as exc:  # noqa: BLE001 —— 作为工具结果回给主模型，而不是炸掉整轮
            return f"错误：子 agent 不可用（{type(exc).__name__}: {exc}）"
        started = time.time()
        _emit({"kind": "subagent", "phase": "start", "task": text[:200],
               "sub_session": session_id})
        try:
            result = agent.run(text, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.time() - started) * 1000)
            msg = f"错误：子任务执行失败（{type(exc).__name__}: {exc}）"
            _record({"ts": started, "sub_session": session_id, "task": text,
                     "ok": False, "error": str(exc), "elapsed_ms": elapsed})
            _emit({"kind": "subagent", "phase": "end", "ok": False,
                   "sub_session": session_id, "elapsed_ms": elapsed,
                   "output": msg})
            return msg
        output = (result.content or "").strip()
        if not output:
            # 子 agent 常在「连续工具调用」后不回文字（与主对话同源问题）：
            # 补一次「只总结、不许调工具」的收尾轮，尽量拿到结论
            try:
                wrap = agent.run(
                    "请直接用一段话总结你刚才完成的工作与结论，不要再调用任何工具。",
                    session_id=session_id)
                output = (wrap.content or "").strip()
            except Exception:  # noqa: BLE001 —— 收尾失败退回空结论提示
                output = ""
        elapsed = int((time.time() - started) * 1000)
        steps = list(getattr(result, "tool_calls", []) or [])
        step_names = [getattr(s, "name", "") or (s.get("name") if isinstance(s, dict) else "")
                      for s in steps]
        if not step_names:
            step_names = list(tracer.tools)  # 兜底：tracer 跨轮累计的工具名
        if not output:
            # 指出第一个失败的工具结果，比一句「未给出结论」有用得多
            first_err = next((t for t in tracer.trace
                              if str(t.get("result") or "").startswith("错误")), None)
            hint = ("首个失败：" + first_err["name"] + " → " + first_err["result"][:120]
                    if first_err else "")
            output = ("（子 agent 未给出文字结论；执行了 " + str(len(step_names)) +
                      " 次工具调用：" + "、".join([n for n in step_names if n][:8]) +
                      ("；" + hint if hint else "") +
                      "。可让它「把结论写成一句话再返回」重试。）")
        _record({"ts": started, "sub_session": session_id, "task": text, "ok": True,
                 "output": output[:4000], "elapsed_ms": elapsed,
                 "steps": [n for n in step_names if n],
                 "trace": tracer.trace[:60]})
        _emit({"kind": "subagent", "phase": "end", "ok": True,
               "sub_session": session_id, "elapsed_ms": elapsed,
               "output": output[:600], "steps": [n for n in step_names if n]})
        return output

    ctx.tools.register(spawn_subagent)

    def factory():
        """供其它插件/界面复用的子 agent 工厂（惰性）。"""
        return _sub_agent()

    ctx.provide("subagent_factory", factory)
