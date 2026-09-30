"""定时任务插件（功能5）：后台 daemon 线程到点执行提示词，结果落盘。

任务存 ``<profile>/scheduled/tasks.json``（由 :mod:`harness.schedule_store` 读写，
``sha schedule add/list/remove`` 与本插件共用同一份）。worker 每轮 tick 重新读盘，
因此 CLI 增删任务对运行中的 harness 立即生效。

线程安全的关键决策：
- **执行在独立的单线程 worker 里串行进行**，且每轮任务都**新建独立 Agent**
  （全新 Memory）——绝不复用主界面那个 agent：chat_loop 的 agent 载着用户的
  会话历史，定时任务混进去会串味；两处并发共用一个 Agent 也有状态竞争。
- 任务执行期间不再做下一轮到期检查（单线程天然串行）：宁可推迟别的任务，
  也不并发执行任务（工具副作用不可重入）。
- 结果**追加**写 ``<profile>/scheduled/<名>.log``，出错也写（含错误摘要），
  事后可查每次执行情况。

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from harness.schedule_store import due_tasks, load_tasks, scheduled_dir

TICK_SECONDS = 1.0  # 到期检查的粒度；任务最小间隔 1 秒（schedule_store 校验）


def register(ctx) -> None:
    host = ctx.host
    stop_event = threading.Event()

    def _build_runtime():
        """为任务执行构建一套独立运行时（模型 + 工具 + 技能），不复用主 agent。"""
        from harness.builtins.chat_loop.register import _max_iterations
        from nanoagent import Agent
        from nanoagent.memory import Memory
        from nanoagent.skills import SkillRegistry

        runtime = host.service("models_runtime") or {}
        pool = runtime.get("pool") or {}
        if not pool:
            raise RuntimeError("没有可用模型，无法执行定时任务")
        llm = pool[runtime["current"]]

        registry = SkillRegistry()
        for skill_dir in host.collect_skill_dirs():
            registry.add_dir(skill_dir)

        rules = host.service("guardrails") or {}
        agent = Agent(name="定时任务", instructions="你是定时任务执行器：执行给定任务并给出简明结果。",
                      llm=llm, tools=host.collect_tools(), memory=Memory(),
                      max_iterations=_max_iterations(host.profile.load_config()),
                      input_guardrails=rules.get("input") or None,
                      output_guardrails=rules.get("output") or None, tracer=None)
        if len(registry):
            agent.enable_skills(registry)
        return agent

    def _run_task(task: dict) -> str:
        """执行一个任务，返回给日志的一行摘要。"""
        from harness.schedule_store import write_back_result

        name = str(task.get("name"))
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            agent = _build_runtime()
            result = agent.run(str(task.get("prompt") or ""), session_id=f"scheduled-{name}")
            content = str(result.content or "（空回复）")
            lines = [f"[{started}] 完成", content, ""]
            summary = f"{name}: 完成（{len(content)} 字）"
            # 结果回流：task.session 指定的会话追加本轮结果（桌面端可见）
            back = None
            try:
                busy = getattr(host, "is_session_busy", None)
                back = write_back_result(host.profile, task, content, is_busy=busy)
            except Exception as exc:  # noqa: BLE001 —— 回流失败不影响任务本身
                lines.append(f"（回流会话失败: {type(exc).__name__}: {exc}）")
            if back:
                # 会话文件已变：让 chat_loop 丢弃内存里的旧历史（下轮按盘回放）
                reset = host.service("new_session")
                if callable(reset):
                    reset(str(back["session"]))
                emit = getattr(host, "emit_agent_event", None)
                if callable(emit):
                    try:
                        emit({"kind": "schedule_done", "task": name,
                              "session": str(back["session"])})
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001 —— 单个任务失败不能弄死调度线程
            lines = [f"[{started}] 失败：{type(exc).__name__}: {exc}", ""]
            summary = f"{name}: 失败（{type(exc).__name__}）"
        log_path = scheduled_dir(host.profile) / f"{name}.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass  # 日志写不进不影响任务本身
        return summary

    def worker() -> None:
        # 读盘失败按空列表处理：tasks.json 被手改坏的瞬间调度线程不能崩
        while not stop_event.is_set():
            try:
                tasks = load_tasks(host.profile)
            except Exception:  # noqa: BLE001
                tasks = []
            # 单线程串行：执行期间不做下一轮检查（宁可推迟也不并发，副作用不可重入）
            for task in due_tasks(tasks, time.time()):
                summary = _run_task(task)
                # 回写 last_run：此时再读一次盘，避免覆盖 CLI 期间的增删
                try:
                    fresh = load_tasks(host.profile)
                    for item in fresh:
                        if str(item.get("name")) == str(task.get("name")):
                            item["last_run"] = time.time()
                            item.setdefault("runs", []).append(summary)
                    from harness.schedule_store import save_tasks

                    save_tasks(host.profile, fresh)
                except Exception:  # noqa: BLE001 —— 记账失败不影响已执行的任务
                    pass
            stop_event.wait(TICK_SECONDS)

    thread = threading.Thread(target=worker, name="sha-scheduler", daemon=True)

    def stop() -> None:
        stop_event.set()
        thread.join(timeout=5)

    def _start() -> Callable[[], None]:
        """effect 的 fn：启动线程并返回 disposer（卸载时 LIFO 回滚，H-06 语义）。"""
        thread.start()
        return stop

    ctx.effect(_start)
