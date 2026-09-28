"""TODO.md 工作区任务索引（长程任务架构 F1：环境即状态）。

此前待办清单是**纯内存态**——重启即丢、不在工作区、Git 看不见。
现在清单持久化为工作区根的 ``TODO.md``（Markdown 勾选格式）：

    # TODO

    - [ ] 待办任务
    - [~] 进行中任务 —— 补充说明
    - [x] 已完成任务

人可读、Git 可跟踪、跨会话/跨进程不丢；新会话靠读它即可无损恢复工程状态。
状态常量复用 nanoagent 的 :mod:`~nanoagent.task`，不另造一套。

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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

# Markdown 勾选标记 → 状态（[ ] 空 / [~] 进行中 / [x] 完成 / [-] 跳过）
MARK_BY_CHAR = {
    " ": STATUS_PENDING,
    "~": STATUS_IN_PROGRESS,
    "x": STATUS_DONE,
    "-": STATUS_SKIPPED,
}
_LINE_RE = re.compile(r"^-\s*\[([ x~\-])\]\s*(.+)$")

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


def _todo_path(host) -> Path:
    return Path(host.workspace) / "TODO.md"


def _parse_md(text: str) -> list[TodoItem]:
    """从 TODO.md 文本解析任务项；容错：忽略不认识的行与非法标记。"""
    items: list[TodoItem] = []
    for line in text.splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        status = MARK_BY_CHAR.get(m.group(1))
        if status is None:
            continue
        rest = m.group(2).strip()
        title, sep, detail = rest.partition("——")
        items.append(TodoItem(
            id=len(items) + 1,
            title=title.strip() or rest,
            detail=detail.strip() if sep else "",
            status=status,
        ))
    return items


def _render(items: list[TodoItem]) -> str:
    if not items:
        return "（待办清单为空）"
    done = sum(1 for item in items if item.status in (STATUS_DONE, STATUS_SKIPPED))
    lines = [f"{MARKS.get(item.status, '[ ]')} {item.id}. {item.title}"
             + (f" —— {item.detail}" if item.detail else "")
             for item in items]
    return f"待办清单（{done}/{len(items)} 已完成）\n" + "\n".join(lines)


def _render_md(items: list[TodoItem]) -> str:
    lines = ["# TODO", ""]
    for item in items:
        lines.append(f"- {MARKS.get(item.status, '[ ]')} {item.title}"
                     + (f" —— {item.detail}" if item.detail else ""))
    lines.append("")
    return "\n".join(lines)


def register(ctx) -> None:
    host = ctx.host

    def _load() -> list[TodoItem]:
        path = _todo_path(host)
        if not path.is_file():
            return []
        try:
            return _parse_md(path.read_text(encoding="utf-8"))
        except OSError:
            return []

    def _save(items: list[TodoItem]) -> str:
        path = _todo_path(host)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_render_md(items), encoding="utf-8")
        except OSError as exc:
            return f"错误：TODO.md 写入失败（{exc}）"
        return _render(items)

    def todo_write(items: list) -> str:
        """写入（覆盖）工作区 TODO.md 任务清单，用于规划与更新进度。

        每次提交**完整**清单（不是增量），这样它能真实反映现状；
        完成一个原子任务后请立即更新对应条目的状态。

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
            return _save([])
        if len(items) > MAX_ITEMS:
            return f"错误：清单最多 {MAX_ITEMS} 项（收到 {len(items)} 项），请合并同类型步骤"

        parsed: list[TodoItem] = []
        for index, raw_entry in enumerate(items, start=1):
            entry = {"content": raw_entry} if isinstance(raw_entry, str) else raw_entry
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
        return _save(parsed)

    def todo_read() -> str:
        """查看工作区 TODO.md 任务清单与进度（跨会话持久）。"""
        return _render(_load())

    ctx.tools.register(todo_write)
    ctx.tools.register(todo_read)
    # 暴露给界面（桌面端「✅ 任务」面板直接读工作区 TODO.md 文件本体）
    ctx.provide("todos", {"path": lambda: str(_todo_path(host))})
