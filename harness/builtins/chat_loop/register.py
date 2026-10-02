"""对话循环插件：懒构建 nanoagent Agent（等所有插件激活后再聚合工具）。"""

from __future__ import annotations

import json
import time
from typing import Any

INSTRUCTIONS = (
    "你是卅 harness 的中文编程助手，简洁、诚实、善用工具。"
    "无论用户用什么语言提问，回复必须始终使用简体中文："
    "解释、结论、列表、代码注释全部用中文，代码标识符和必要的英文术语可以保留原文。"
    "回答「工作区里有什么 / 目录结构 / 某文件内容」这类问题时，"
    "必须现场调用 list_files / read_file 实际查看当前工作区后再回答，"
    "不要凭会话历史里的旧清单或旧印象作答——用户可能已经切换了工作区。"
)

# 长程任务执行准则（F2）：方法论三原则（环境即状态 / 串行优于并发 / TDD 默认失败）。
# config.longrun.guidelines = false 可整体关闭。
LONGRUN_GUIDELINES = (
    "\n\n## 长程任务执行准则\n"
    "- 原子任务最小粒度：一次只实现一个函数/接口，避免大范围重构。\n"
    "- 串行推进：动手前先读工作区 TODO.md 与 git 历史；绝不并发修改同一文件。\n"
    "- TDD 默认失败：先写/运行测试确认用例失败，再实现代码直至全绿。\n"
    "- 提交与推送由用户手动完成：测试全绿后向用户**报告建议的提交信息**，"
    "不要自行调用 git_add/git_commit（用户明确要求且已开放时除外）。\n"
    "- 每完成一个原子任务立即用 todo_write 更新 TODO.md。\n"
    "- 关键状态写文件、进度写 TODO.md，不要依赖会话记忆保存长期状态。\n"
)


# 计划模式（用户要求：实现任务前先构建计划）。config.plan_mode 开关；
# 切换时 set_plan_mode 会重建 agent（记忆按会话回放，不丢上下文）。
PLAN_MODE_INSTRUCTIONS = (
    "\n\n## 计划模式（当前开启）\n"
    "在动手实现任何任务之前，必须**先输出实现计划**并等待用户确认：\n"
    "1. 目标与验收标准（做到什么算完成）\n"
    "2. 步骤清单（编号，每步一个原子任务）\n"
    "3. 涉及的文件与接口\n"
    "4. 风险与不确定点（需要用户拍板的选项要明确列出）\n"
    "用户确认（回复「开始」或明确同意）后：先用 todo_write 把步骤写入 TODO.md，"
    "再逐个原子任务实现；实现过程中若需要偏离计划，先说明并征得同意。\n"
    "注意：git 提交始终由用户手动完成，你只负责代码与测试。\n"
)


def _recovery_context(host) -> str:
    """F3：新会话恢复上下文——TODO.md 未完成项 + 最近 git 提交。

    有什么读什么（TODO.md 缺失 / 非 git 仓库都静默跳过），
    两者皆空返回空串（调用方据此跳过注入）。
    """
    import subprocess
    from pathlib import Path

    from harness.procutil import CREATE_NO_WINDOW

    lines: list[str] = []
    todo_path = Path(host.workspace) / "TODO.md"
    try:
        if todo_path.is_file():
            for line in todo_path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith(("- [ ]", "- [~]")):
                    lines.append(s)
    except OSError:
        pass

    log: list[str] = []
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", "log", "--oneline", "-5"],
            cwd=str(host.workspace), capture_output=True, timeout=10, check=False,
            creationflags=CREATE_NO_WINDOW)
        if proc.returncode == 0:
            raw = proc.stdout
            text = raw.decode("utf-8", "replace")
            if "\ufffd" in text:  # Windows 中文提交信息常为 GBK，兜一次编码
                try:
                    text = raw.decode("gbk", "replace")
                except LookupError:
                    pass
            log = [line for line in text.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired):
        pass

    if not lines and not log:
        return ""
    parts = []
    if lines:
        parts.append("未完成任务：\n" + "\n".join(lines))
    if log:
        parts.append("最近提交：\n" + "\n".join(log))
    return "\n".join(parts)


def _attach_reasoning(history: list, reasoning) -> None:
    """把本轮思考过程附到最后一条 assistant 消息上（就地修改传入副本）。

    ``Memory.history`` 返回的是全新字典副本，附加 ``reasoning`` 键不会污染
    内存里的记忆；会话文件原样保存该键，桌面端回放会话时据此重建
    「深度思考」折叠块——否则二次进入会话就看不到思考过程了。
    """
    text = str(reasoning or "").strip()
    if not text:
        return
    for item in reversed(history):
        if item.get("role") == "assistant":
            item["reasoning"] = text[:20000]
            return


# 预设模式（用户不可改，可新建自定义模式补充）：name → 附加提示词 / 是否禁用工具。
# current_mode 为空或 "默认" 即不附加任何东西（与既往行为一致）。
PRESET_MODES: dict[str, dict] = {
    "默认": {},
    "计划": {"instructions": PLAN_MODE_INSTRUCTIONS},
    "快速回答": {
        "instructions": ("\n\n## 快速回答模式（当前开启）\n"
                         "直接给出简洁答案：不要调用任何工具，不要长篇展开；"
                         "能一句话说清就不分段。"),
        "disable_tools": True,
    },
    "只读分析": {
        "instructions": ("\n\n## 只读分析模式（当前开启）\n"
                         "只做阅读、分析与建议：不要写入/修改/删除任何文件，"
                         "不要执行会产生副作用的命令；需要动手时先征求用户确认。"),
    },
    "安全研究": {
        "instructions": (
            "\n\n## 安全研究模式（当前开启）\n"
            "用户是安全从业者，当前工作处于**已授权**范围内：防御性分析、有书面授权的\n"
            "渗透测试、CTF 解题、恶意样本行为分析、安全事件研判与报告撰写。\n"
            "- 可以：讲解漏洞原理与利用思路（教学视角）、编写加固建议、分析样本/日志、\n"
            "  撰写测试用例与复现步骤（仅针对授权靶标）。\n"
            "- 涉及具体 payload 时优先给教学级解释 + 防御视角，避免可直接投递的成品。\n"
            "- 对未授权的真实目标、破坏性操作或窃取数据的请求：拒绝并说明原因。\n"
            "- 本模式只是声明工作语境，不改变模型服务商自身的安全策略。"),
    },
    "知识库问答": {
        "instructions": (
            "\n\n## 知识库问答模式（当前开启）\n"
            "回答任何问题前，必须先用 search_knowledge 工具检索本地知识库。\n"
            "- 回答**只基于**检索到的内容，逐条用 [编号]（来源: 文件名） 标注出处。\n"
            "- 知识库没有覆盖的部分，明确说「知识库中未涵盖」，不要凭记忆补充。\n"
            "- 知识库为空时，提示用户先在「📚 知识库」面板索引文档再提问。"),
    },
}


def _resolve_mode(config: dict) -> dict:
    """按 config.current_mode 解析出模式定义（预设 + 用户自定义，查不到回默认）。"""
    name = str(config.get("current_mode") or "默认").strip() or "默认"
    if name in PRESET_MODES:
        mode = dict(PRESET_MODES[name])
        mode["name"] = name
        return mode
    for item in config.get("user_modes") or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip() == name:
            return {"name": name,
                    "instructions": "\n\n## " + name + " 模式（当前开启）\n"
                                    + str(item.get("instructions") or ""),
                    "disable_tools": bool(item.get("disable_tools"))}
    return {"name": "默认", "instructions": "", "disable_tools": False}


def _summarize_args(arguments: Any) -> dict:
    """把工具入参压成界面用的小摘要（不把整篇文件内容推给前端）。"""
    if not isinstance(arguments, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("path", "command", "pattern", "cwd", "task", "name", "subdir"):
        val = arguments.get(key)
        if val:
            out[key] = str(val)[:200]
    if "content" in arguments:
        try:
            out["lines"] = str(arguments["content"]).count("\n") + 1
        except Exception:  # noqa: BLE001
            pass
    for key, cap in (("old", 120), ("new", 120)):
        if arguments.get(key):
            out[key] = str(arguments[key])[:cap]
    return out


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
                    "args": _summarize_args(data.get("arguments")),
                })
        except Exception:  # noqa: BLE001 —— 事件推送失败不影响对话
            pass


def _filter_disabled_tools(tools: list, config: dict) -> list:
    """按 config.disabled_tools 过滤工具（桌面端「通用 → Agent 工具」开关）。"""
    disabled = config.get("disabled_tools")
    if not isinstance(disabled, list) or not disabled:
        return tools
    banned = {str(x) for x in disabled}
    return [t for t in tools if getattr(t, "name", "") not in banned]


_DISTILL_STATE = {"rounds": 0}


def _maybe_distill(host, session_id: str) -> None:
    """长期记忆自动沉淀：每 N 轮（config.memory_distill.every_rounds，默认 8）在后台
    让模型从最近对话提炼「值得长期记住」的要点，写成建议稿（memory_suggest.json）
    并推一条通知——采纳与否由用户在 🧠 记忆面板决定，绝不静默改写 memory.md。

    默认**关闭**（config.memory_distill.enabled=true 显式开启）：它会在对话外
    额外发一次 LLM 请求，是否接受这份开销由用户决定。
    """
    import json
    import threading
    import time
    from pathlib import Path

    try:
        cfg = host.profile.load_config()
        md = cfg.get("memory_distill") if isinstance(cfg.get("memory_distill"), dict) else {}
        if md.get("enabled") is not True:
            return
        every = max(1, int(md.get("every_rounds") or 8))
    except Exception:  # noqa: BLE001
        return
    _DISTILL_STATE["rounds"] += 1
    if (_DISTILL_STATE["rounds"] - 1) % every:
        return

    def work() -> None:
        try:
            runtime = host.service("models_runtime") or {}
            llm = (runtime.get("pool") or {}).get(runtime.get("current") or "")
            if llm is None or not callable(getattr(llm, "chat", None)):
                return
            history = host.profile.load_session(session_id)
            transcript = "\n".join(
                f"{m.get('role')}: {str(m.get('content') or '')[:400]}"
                for m in history[-12:] if str(m.get("content") or "").strip())
            if not transcript:
                return
            prompt = ("从下面的对话里提炼「值得长期记住」的信息：用户的偏好、稳定事实、"
                      "项目关键结论。没有值得记的就只输出：无。每条一行、不超过 8 条，"
                      "不要寒暄和过程性内容。\n\n对话：\n" + transcript[:6000])
            text = str(llm.chat([{"role": "user", "content": prompt}]).content or "").strip()
            if not text or text in ("无", "无。"):
                return
            suggest = {"text": text[:2000], "ts": time.time(),
                       "session": session_id}
            path = Path(host.profile.root) / "memory_suggest.json"
            path.write_text(json.dumps(suggest, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            from harness.notifications import push_notification

            push_notification(host.profile, "info",
                              "记忆沉淀建议已生成（🧠 面板可采纳）",
                              text.splitlines()[0][:80] if text else "")
        except Exception:  # noqa: BLE001 —— 沉淀失败绝不影响对话
            pass

    threading.Thread(target=work, daemon=True, name="sha-distill").start()


def _max_iterations(config: dict) -> int:
    """单轮回答允许的最大 模型↔工具 循环数（config.max_iterations，默认 10）。

    这是防死循环的安全阀：模型连续请求工具而不给最终回答时，到顶即停，
    避免无限烧 token。桌面端「通用」页可调（任务重的场景调大即可）。
    """
    raw = config.get("max_iterations")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = 10
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 10
    return max(1, min(value, 200))


def _build_memory(cfg: dict, llm):
    """按 config.json 的 ``memory`` 段构建记忆（缺省=全量 Memory，行为与既往一致）。

    - ``{"type": "summary", "max_tokens": 24000}``：SummaryMemory——历史超窗时
      自动把最旧消息压缩成摘要（用当前对话的 LLM），只保留摘要 + 最近几条。
      长会话的 token 成本从线性增长变为近似封顶。
    - ``{"type": "sliding", "max_messages": 40}``：滑动窗口，超限丢最旧。
    - 缺省 / 其他取值：全量 Memory（不裁剪）。

    summary 未写 max_tokens 时按当前模型 context_window 的 ~55% 推导
    （给回复与系统提示词留余量），模型窗口未知则退回 24000。
    """
    from nanoagent.memory import Memory, SummaryMemory

    mtype = str(cfg.get("type") or "full").lower()
    if mtype == "summary":
        max_tokens = int(cfg.get("max_tokens") or 0)
        if max_tokens <= 0:
            window = int(getattr(llm, "context_window", 0) or 0)
            max_tokens = int(window * 0.55) if window > 0 else 24000
        return SummaryMemory(
            max_tokens=max_tokens,
            keep_recent=int(cfg.get("keep_recent") or 6),
            llm=llm,
        )
    if mtype == "sliding":
        return Memory(max_messages=int(cfg.get("max_messages") or 40))
    return Memory()


def _extract_cached_tokens(usage: dict) -> int:
    """从用量字典里提取缓存命中 tokens（不同服务商字段位置不同，防御式提取）。"""
    usage = usage or {}
    for key in ("cached_tokens", "prompt_cache_hit_tokens"):
        if usage.get(key):
            return int(usage[key])
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens"):
        return int(details["cached_tokens"])
    return 0


def _estimate_usage(agent, session_id: str, message: str, reply: str) -> dict:
    """服务商不回 usage 帧时的估算兜底（nanoagent 的 estimate_tokens 启发式）。"""
    from nanoagent.memory import estimate_tokens

    prompt = estimate_tokens(message)
    try:
        for m in agent.memory.history(session_id):
            prompt += estimate_tokens(str(m.get("content") or ""))
    except Exception:  # noqa: BLE001 —— 估算失败就只算本轮
        pass
    return {"prompt_tokens": prompt,
            "completion_tokens": estimate_tokens(reply or "")}


def _record_usage(host, session_id: str, model: str, usage: dict,
                  fallback: dict | None = None) -> None:
    """每轮对话追加一条用量记录到 <profile>/usage.jsonl（`sha usage` 的数据源）。

    服务商流式响应不带 usage 帧时（部分 OpenAI 兼容端点如此），改用
    ``fallback``（基于上下文的估算值）并标记 ``estimated: true``——
    统计不缺席，只是精度降级。
    """
    usage = usage or {}
    rec = {"ts": time.time(), "session": session_id, "model": model,
           "prompt_tokens": int(usage.get("prompt_tokens") or 0),
           "completion_tokens": int(usage.get("completion_tokens") or 0)}
    if not rec["prompt_tokens"] and not rec["completion_tokens"] and fallback:
        rec = {**fallback, "ts": rec["ts"], "session": session_id,
               "model": model, "estimated": True}
    cached = _extract_cached_tokens(usage)
    if cached:
        rec["cached_tokens"] = cached
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

    def _compose_instructions() -> str:
        """主系统提示词装配（get_agent 与 build_agent_with 共用，单一事实来源）。

        叠加顺序：内置指令 ← 全局人设（persona.md）← 工作区规则（AGENT.md，
        跟代码走可提交 git）← 长期记忆（memory.md）← 长程准则/恢复/计划模式。
        """
        from pathlib import Path

        config = host.profile.load_config()
        instructions = INSTRUCTIONS
        # 全局人设（桌面端 🧠 面板上半区编辑）
        persona_path = Path(host.profile.root) / "persona.md"
        if persona_path.is_file():
            try:
                persona = persona_path.read_text(encoding="utf-8").strip()[:4000]
            except OSError:
                persona = ""
            if persona:
                instructions += "\n\n## 人设（用户维护，始终生效）\n" + persona
        # 工作区级规则：AGENT.md 放在工作区根，跟项目走（git 提交后团队共享）
        try:
            agent_md = (Path(host.workspace) / "AGENT.md")
            if agent_md.is_file():
                rules = agent_md.read_text(encoding="utf-8").strip()[:6000]
                if rules:
                    instructions += "\n\n## 工作区规则（AGENT.md）\n" + rules
        except OSError:
            pass
        # 长期记忆（profile/memory.md，桌面端 🧠 面板维护）：跨会话注入
        memory_path = Path(host.profile.root) / "memory.md"
        if memory_path.is_file():
            try:
                memory_text = memory_path.read_text(encoding="utf-8").strip()[:4000]
            except OSError:
                memory_text = ""
            if memory_text:
                instructions += "\n\n## 长期记忆（用户维护，跨会话生效）\n" + memory_text
        # 当前模式（预设/自定义）的附加提示词
        instructions += str(_resolve_mode(config).get("instructions") or "")
        # F2/F3：长程执行准则 + 跨会话状态恢复（config.longrun.* 可分别关闭）
        longrun = config.get("longrun") if isinstance(config.get("longrun"), dict) else {}
        if longrun.get("guidelines") is not False:
            instructions += LONGRUN_GUIDELINES
        if longrun.get("recovery") is not False:
            recovery = _recovery_context(host)
            if recovery:
                instructions += ("\n\n## 当前工程状态（跨会话自动恢复；"
                                 "继续任务前先复核 TODO.md）\n" + recovery)
        if config.get("plan_mode"):
            instructions += PLAN_MODE_INSTRUCTIONS
        # 当前工作区显式声明：桌面端支持热切换，模型必须知道自己被问的是哪个目录
        instructions += f"\n\n当前工作区：{host.workspace}"
        return instructions

    def _build_skills_registry():
        from pathlib import Path

        from nanoagent.skills import SkillRegistry

        registry = SkillRegistry()
        for skill_dir in host.collect_skill_dirs():
            registry.add_dir(skill_dir)
        profile_skills = Path(host.profile.root) / "skills"  # 用户安装的技能
        if profile_skills.is_dir():
            registry.add_dir(profile_skills)
        # 用户禁用的技能（config.disabled_skills，桌面端技能页开关）：按名移除
        disabled = host.profile.load_config().get("disabled_skills")
        if isinstance(disabled, list):
            for name in disabled:
                registry.remove(str(name))
        return registry

    def _make_ask_user_tool():
        """构造 ask_user 工具：agent 拿不准时向用户提问（桌面端弹问答卡片）。

        没有交互界面（CLI 一次性执行/管道）或用户超时未答时，返回提示文本
        让模型按最合理假设继续——工具绝不能卡死对话。
        """
        from nanoagent.tools import tool as tool_decorator

        @tool_decorator(
            name="ask_user",
            description="向用户提出关键决策问题（方案选型/缺必要参数/需要拍板）并等待回答。"
                        "问题要具体、给出你的推荐选项；仅在真正影响后续走向时使用，不要频繁打扰。",
        )
        def ask_user(question: str, options: str = "", timeout_seconds: int = 180) -> str:
            """向用户提问并等待回答。

            Args:
                question: 要问的问题（一句话说清背景与选项差异）
                options: 可选候选项，用 | 分隔（如 "A 方案|B 方案|都不对"）
                timeout_seconds: 最长等待秒数（10~600，超时按未回答处理）
            """
            sink = getattr(host, "ask_user_sink", None)
            if not callable(sink):
                return ("（当前环境没有交互界面，无法提问）"
                        "请按最合理假设继续，并在回复中明确说明你所做的假设。")
            try:
                timeout = max(10, min(int(timeout_seconds or 180), 600))
            except (TypeError, ValueError):
                timeout = 180
            try:
                return str(sink(question=str(question or ""), options=str(options or ""),
                               timeout=timeout))
            except Exception as exc:  # noqa: BLE001 —— 提问失败不拖垮对话
                return f"提问失败（{type(exc).__name__}），请按最合理假设继续并说明假设。"

        return ask_user

    def get_agent():
        if state["agent"] is None:
            from nanoagent import Agent

            runtime = host.service("models_runtime") or {}
            pool = runtime.get("pool") or {}
            if not pool:
                raise RuntimeError("没有可用模型：请在 .harness/profiles/<名>/config.json 的 models 里配置")
            llm = pool[runtime["current"]]

            config = host.profile.load_config()
            instructions = _compose_instructions()
            memory = _build_memory(config.get("memory") or {}, llm)
            session_id = state["session_id"]
            for item in host.profile.load_session(session_id):
                if item.get("role") in ("user", "assistant"):
                    memory.add(session_id, item["role"], item.get("content", ""))

            # 护栏与 tracer 都由插件提供，缺席即不启用（软依赖，不写进 inject）
            rules = host.service("guardrails") or {}
            make_tracer = (host.service("tracing") or {}).get("new_tracer")

            tools = host.collect_tools()
            # 工具黑白名单：config.disabled_tools 里的工具名不进 agent
            tools = _filter_disabled_tools(tools, config)
            # ask_user：agent 主动向用户提问（config.ask_user=false 关闭）
            if config.get("ask_user") is not False:
                tools = [*tools, _make_ask_user_tool()]

            agent = Agent(name="卅助手", instructions=instructions,
                          llm=llm,
                          tools=tools if not _resolve_mode(config).get("disable_tools") else [],
                          memory=memory,
                          max_iterations=_max_iterations(config),
                          input_guardrails=rules.get("input") or None,
                          output_guardrails=rules.get("output") or None,
                          tracer=make_tracer() if callable(make_tracer) else None)
            agent.tracer = _EventTracer(agent.tracer, host)
            registry = _build_skills_registry()
            if len(registry):
                agent.enable_skills(registry)
            state["agent"] = agent
        return state["agent"]

    def build_agent_with(llm, session_id: str, tools=None):
        """构建一个使用**指定 llm** 的独立 agent（并答对比等一次性场景）。

        复用主指令装配与技能注册；tools 缺省=主工具集，传 [] 得到纯对话 agent。
        不绑定主 agent 状态（state），也不挂事件 tracer——调用方自管生命周期。
        """
        from nanoagent import Agent

        memory = _build_memory(host.profile.load_config().get("memory") or {}, llm)
        for item in host.profile.load_session(session_id):
            if item.get("role") in ("user", "assistant"):
                memory.add(session_id, item["role"], item.get("content", ""))
        rules = host.service("guardrails") or {}
        agent = Agent(name="卅助手", instructions=_compose_instructions(), llm=llm,
                      tools=host.collect_tools() if tools is None else list(tools),
                      memory=memory,
                      max_iterations=_max_iterations(host.profile.load_config()),
                      input_guardrails=rules.get("input") or None,
                      output_guardrails=rules.get("output") or None,
                      tracer=None)
        registry = _build_skills_registry()
        if len(registry):
            agent.enable_skills(registry)
        return agent

    def ask_with(message: str, session_id: str | None = None, images: list | None = None,
                 model_name: str | None = None):
        """指定模型执行一轮对话（辅助对话选模型用）。

        模型为空/等于当前模型 → 直接走全局 agent（与 ask 完全同路径）；
        其他模型 → 经 build_agent_with 按会话回放构建一次性 agent（不缓存、
        不动全局 state），会话照常落盘与计用量。
        """
        runtime = host.service("models_runtime") or {}
        name = str(model_name or "").strip()
        if not name or name == str(runtime.get("current") or ""):
            return ask(message, session_id=session_id, images=images)
        pool = runtime.get("pool") or {}
        if name not in pool:
            raise RuntimeError(f"模型不存在或不可用: {name}（可用: {', '.join(sorted(pool))}）")
        sid = session_id or state["session_id"]
        agent = build_agent_with(pool[name], sid)
        result = agent.run(message, session_id=sid, images=_validate_images(images))
        history = agent.memory.history(sid)
        _attach_reasoning(history, getattr(result, "reasoning", ""))
        host.profile.save_session(sid, history)
        _record_usage(host, sid, name, result.usage,
                      fallback=_estimate_usage(agent, sid, message, result.content or ""))
        hook_text = run_hooks()
        reply = result.content or ""
        if hook_text:
            reply += hook_text
        return {"reply": reply, "reasoning": getattr(result, "reasoning", ""),
                "tool_calls": result.tool_calls, "model": name, "usage": result.usage}

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

    def _stream_events(agent, message: str, session_id: str, images: list | None = None,
                       should_stop=None):
        """同步流式：直接透传 ``run_stream`` 的事件（delta / tool_call / done）。

        工具是同步执行的（MCP 已包装为同步闭包，其余协程工具由 nanoagent
        单独起循环跑完），因此不存在「异步工具要走 arun_stream」的分支——
        那条路径需要 AsyncLLM，而 models 只构建同步 LLM（H-02 的流式面）。

        should_stop: 可选中止钩子，透传给 nanoagent——桌面端「停止」按钮的底座。
        """
        yield from agent.run_stream(message, session_id=session_id, images=images,
                                    should_stop=should_stop)

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
        history = agent.memory.history(session_id)
        _attach_reasoning(history, getattr(result, "reasoning", ""))
        host.profile.save_session(session_id, history)
        runtime = host.service("models_runtime") or {}
        _record_usage(host, session_id, runtime.get("current", ""), result.usage,
                      fallback=_estimate_usage(agent, session_id, message,
                                               result.content or ""))
        hook_text = run_hooks()
        reply = result.content or ""
        if hook_text:
            reply += hook_text
        _maybe_distill(host, session_id)
        return {"reply": reply, "reasoning": getattr(result, "reasoning", ""),
                "tool_calls": result.tool_calls,
                "model": runtime.get("current", ""), "usage": result.usage}

    def ask_stream(message: str, session_id: str | None = None, images: list | None = None,
                   should_stop=None):
        """流式对话：依次产出 ``delta`` / ``tool_call`` / ``done`` 事件。

        ``done`` 事件里的 ``result`` 与 :func:`ask` 同源（完整 AgentResult）。

        should_stop: 可选中止钩子（如桌面端「停止」按钮），透传给 nanoagent；
        中止后生成器不再产出 done，消费方按「未完成」处理（不落盘，与中断一致）。

        会话落盘**由本函数在流结束时补上**：nanoagent 的 ``run_stream`` 只把回合
        记进内存里的 agent.memory，并不写 profile 的会话文件（那是 harness 的职责，
        ``ask`` 里显式调 ``save_session``）。此前这里漏了这步，于是流式跑完关掉
        界面，这一轮对话就凭空消失了。顺带在 done 事件里补记 token 用量
        （此前只有整段 ask 记录，流式轮次在 usage.jsonl 里是空白）。

        模型不支持 ``chat_stream`` 时会抛错 —— 调用方应先用 :func:`can_stream`
        判断，不要「先试再回退」（跑到一半失败会有重复的工具副作用）。
        """
        session_id = _bind_session(session_id)
        agent = get_agent()
        final_result = None
        for event in _stream_events(agent, message, session_id, _validate_images(images),
                                    should_stop=should_stop):
            if event.get("type") == "done":
                final_result = event.get("result")
            yield event
        # 消费方提前 break 时生成器被关闭，这里不会执行（与 ask 中断即不落盘一致）
        history = agent.memory.history(session_id)
        _attach_reasoning(history, getattr(final_result, "reasoning", "") if final_result else "")
        host.profile.save_session(session_id, history)
        if final_result is not None:
            runtime = host.service("models_runtime") or {}
            _record_usage(host, session_id, runtime.get("current", ""),
                          getattr(final_result, "usage", None) or {},
                          fallback=_estimate_usage(agent, session_id, message,
                                                   final_result.content or ""))
            _maybe_distill(host, session_id)

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

    def set_plan_mode(on: bool) -> str:
        """切换计划模式（桌面端 📋 计划按钮 / REPL /plan）。

        立即重建 agent 以应用新指令——记忆按会话回放，上下文不丢。
        """
        host.profile.update_config(plan_mode=bool(on))
        state["agent"] = None
        if on:
            return ("计划模式已开启：实现类任务会先输出计划并等你确认，"
                    "确认后自动写入 TODO.md 再动手")
        return "计划模式已关闭"

    def reset_agent() -> None:
        """丢弃当前 agent（下轮 ask 重建）：MCP/插件/工具集变更后刷新用。

        只清 agent 不动会话 id——记忆按会话回放，上下文不丢。
        """
        state["agent"] = None

    def run_hooks() -> str:
        """执行 config.hooks.reply_end 命令（每轮回复结束后），返回附在回复后的文本。

        形如 {"hooks": {"reply_end": ["pytest -q", "git status --short"]}}；
        命令在工作区目录 shell 执行，超时 180s，输出取尾部。hooks 是用户写进
        自己配置的受信命令（与 CI 脚本同性质），不走 shell 权限门；执行失败
        只附说明，绝不影响本轮回复。
        """
        import subprocess

        from harness.procutil import CREATE_NO_WINDOW

        try:
            hooks = host.profile.load_config().get("hooks")
        except Exception:  # noqa: BLE001
            return ""
        cmds = hooks.get("reply_end") if isinstance(hooks, dict) else None
        if not cmds:
            return ""
        if isinstance(cmds, str):
            cmds = [cmds]
        parts: list[str] = []
        for cmd in cmds:
            cmd = str(cmd or "").strip()
            if not cmd:
                continue
            try:
                proc = subprocess.run(cmd, shell=True, cwd=str(host.workspace),
                                      capture_output=True, timeout=180, check=False,
                                      creationflags=CREATE_NO_WINDOW)
                raw = (proc.stdout or b"") + (proc.stderr or b"")
                text = raw.decode("utf-8", "replace")
                if "\ufffd" in text:  # Windows 中文输出常为 GBK，兜一次
                    try:
                        text = raw.decode("gbk", "replace")
                    except LookupError:
                        pass
                tail = "\n".join(text.strip().splitlines()[-40:])[:2000]
                parts.append(f"$ {cmd} → 退出码 {proc.returncode}"
                             + (f"\n{tail}" if tail else ""))
            except Exception as exc:  # noqa: BLE001
                parts.append(f"$ {cmd} → 执行失败: {type(exc).__name__}: {exc}")
        if not parts:
            return ""
        return "\n\n---\n[自动钩子]\n" + "\n\n".join(parts)

    ctx.provide("set_plan_mode", set_plan_mode)
    ctx.provide("reset_agent", reset_agent)
    ctx.provide("build_agent_with", build_agent_with)
    ctx.provide("run_hooks", run_hooks)
    ctx.provide("ask_with", ask_with)

    ctx.provide("agent_factory", get_agent)
    ctx.provide("ask", ask)
    ctx.provide("ask_stream", ask_stream)
    ctx.provide("can_stream", can_stream)
    ctx.provide("new_session", new_session)
    # 同一实现、更贴调用点的名字：明确「切到某个已有会话」时用它
    ctx.provide("switch_session", new_session)
    ctx.provide("current_session", current_session)
