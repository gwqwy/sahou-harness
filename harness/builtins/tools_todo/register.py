"""待办清单插件：把「计划」也变成一个工具。

为什么需要：多步任务里 agent 没有地方记「我做到哪了」，于是要么漏掉步骤，
要么在长上下文里反复重新规划。有了显式清单，它可以先列步骤、逐条推进、
随时回看进度。

数据结构直接复用 nanoagent 的 :class:`~nanoagent.task.TodoItem` 与状态常量，
不另造一套 —— 将来若接 ``TaskRunner``（LLM 自主拆解并执行）也能共用同一表示。

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
"""

from __future__ import annotations

import json

from nanoagent.task import (
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    STATUS_SKIPPED,
    TodoItem,
)

MARKS = {
    STATUS_PENDING: "[ ]",
    STATUS_IN_PROGRESS: "[~]",
    STATUS_DONE: "[x]",
    STATUS_SKIPPED: "[-]",
}

# 容忍模型/用户用同义词表达状态
STATUS_ALIASES = {
    "pending": STATUS_PENDING, "todo": STATUS_PENDING, "open": STATUS_PENDING,
    "待办": STATUS_PENDING, "未开始": STATUS_PENDING, "未完成": STATUS_PENDING,
    "in_progress": STATUS_IN_PROGRESS, "in-progress": STATUS_IN_PROGRESS,
    "doing": STATUS_IN_PROGRESS, "active": STATUS_IN_PROGRESS, "current": STATUS_IN_PROGRESS,
    "进行中": STATUS_IN_PROGRESS, "正在做": STATUS_IN_PROGRESS,
    "done": STATUS_DONE, "completed": STATUS_DONE, "complete": STATUS_DONE,
    "finished": STATUS_DONE, "完成": STATUS_DONE, "已完成": STATUS_DONE,
    "skipped": STATUS_SKIPPED, "skip": STATUS_SKIPPED, "跳过": STATUS_SKIPPED,
}

MAX_ITEMS = 50


def _as_int(value, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def register(ctx) -> None:
    state: dict = {"items": []}

    def _render() -> str:
        items = state["items"]
        if not items:
            return "（待办清单为空）"
        done = sum(1 for item in items if item.status in (STATUS_DONE, STATUS_SKIPPED))
        lines = [f"{MARKS.get(item.status, '[ ]')} {item.id}. {item.title}"
                 + (f" —— {item.detail}" if item.detail else "")
                 for item in items]
        return f"待办清单（{done}/{len(items)} 已完成）\n" + "\n".join(lines)

    def todo_write(items: list) -> str:
        """写入（覆盖）当前的待办清单，用于规划与更新进度。

        每次提交**完整**清单（不是增量），这样它能真实反映现状。

        Args:
            items: 清单数组，每项形如
                {"content": "步骤描述", "status": "pending|in_progress|done|skipped", "detail": "可选补充"}
        """
        if isinstance(items, str):
            try:  # 模型偶尔会把整个数组当字符串传进来
                items = json.loads(items)
            except ValueError:
                return "错误：items 需要是数组（JSON 解析失败）"
        if not isinstance(items, list):
            return "错误：items 需要是数组，每项为 {content, status, detail?}"
        if not items:
            state["items"] = []
            return "已清空待办清单"
        if len(items) > MAX_ITEMS:
            return f"错误：清单最多 {MAX_ITEMS} 项（收到 {len(items)} 项），请合并同类型步骤"

        parsed: list[TodoItem] = []
        for index, entry in enumerate(items, start=1):
            if isinstance(entry, str):
                entry = {"content": entry}
            if not isinstance(entry, dict):
                return f"错误：第 {index} 项不是对象（收到 {type(entry).__name__}）"
            title = str(entry.get("content") or entry.get("title") or entry.get("task") or "").strip()
            if not title:
                return f"错误：第 {index} 项缺少 content（步骤描述）"
            raw_status = str(entry.get("status") or STATUS_PENDING).strip().lower()
            status = STATUS_ALIASES.get(raw_status)
            if status is None:
                return (f"错误：第 {index} 项 status 非法（{raw_status}），"
                        f"可用: pending / in_progress / done / skipped")
            parsed.append(TodoItem(id=index, title=title,
                                   detail=str(entry.get("detail") or "").strip(),
                                   status=status))
        state["items"] = parsed
        return _render()

    def todo_read() -> str:
        """查看当前待办清单与进度。"""
        return _render()

    ctx.tools.register(todo_write)
    ctx.tools.register(todo_read)
    # 暴露给界面（桌面端/REPL 将来可直接渲染进度条）
    ctx.provide("todos", state)
