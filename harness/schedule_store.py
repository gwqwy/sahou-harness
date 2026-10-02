"""定时任务的存储层：tasks.json 的读写与校验（插件与 `sha schedule` CLI 共用）。

存储位置 ``<profile>/scheduled/tasks.json``，形状::

    [{"name": "日报", "every": 3600, "prompt": "总结今天的进展", "enabled": true, "last_run": 0}]

设计要点：
- 任务名是日志文件名（``<profile>/scheduled/<名>.log``），必须走白名单
  （对齐会话 id 的 H-04 教训：来自 CLI/手改 JSON 的名字不可信）。
- 写入一律原子写（atomic_write_text），避免半截文件。
- 插件的 worker 每轮 tick 重新读盘，因此 ``sha schedule add`` 对**正在运行**的
  harness 也生效，不必重启。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .config import Profile, atomic_write_text

# 任务名白名单：字母/数字/下划线开头，允许 . - _ 中文等常见字符，禁路径分隔符
# （故意不放行 / \\ 和首尾空格；log 文件名直接取自它）
TASK_NAME_RE = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9._\-\u4e00-\u9fff]{0,63}")

MIN_EVERY_SECONDS = 1
MAX_EVERY_SECONDS = 30 * 86400

# 钟点制（kind="daily"）：每天在 at="HH:MM" 执行一次
TIME_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")


class ScheduleError(ValueError):
    """任务参数非法（名字不合法 / 间隔超界 / prompt 为空）。"""


def scheduled_dir(profile: Profile) -> Path:
    return Path(profile.root) / "scheduled"


def tasks_path(profile: Profile) -> Path:
    return scheduled_dir(profile) / "tasks.json"


def load_tasks(profile: Profile) -> list[dict[str, Any]]:
    """读取任务列表；文件不存在或损坏时返回空列表（损坏不静默，打印告警由调用方定夺）。"""
    path = tasks_path(profile)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def save_tasks(profile: Profile, tasks: list[dict[str, Any]]) -> Path:
    path = tasks_path(profile)
    atomic_write_text(path, json.dumps(tasks, ensure_ascii=False, indent=2))
    return path


def validate_name(name: str) -> str:
    text = str(name or "").strip()
    if not text or not TASK_NAME_RE.fullmatch(text):
        raise ScheduleError(f"任务名不合法: {name!r}（只允许中英文/数字/._-，不能含路径分隔符）")
    return text


def validate_every(every: Any) -> int:
    try:
        value = int(every)
    except (TypeError, ValueError):
        raise ScheduleError(f"执行间隔必须是秒数（整数），收到: {every!r}") from None
    if not MIN_EVERY_SECONDS <= value <= MAX_EVERY_SECONDS:
        raise ScheduleError(f"执行间隔须在 {MIN_EVERY_SECONDS}~{MAX_EVERY_SECONDS} 秒之间，收到: {value}")
    return value


def normalize_at(at: str) -> str:
    """校验并规整钟点 "H:MM" → "HH:MM"；非法（含小时 >23）抛 ScheduleError。"""
    m = TIME_RE.match(str(at or "").strip())
    if not m or not 0 <= int(m.group(1)) <= 23:
        raise ScheduleError(f'时间格式应为 "HH:MM"（如 "09:30"），收到: {at!r}')
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def daily_target_ts(at: str, now: float) -> float:
    """now 所在**本地日**的 at 时刻对应的时间戳（本地时区，含 DST 处理）。"""
    hh, mm = normalize_at(at).split(":")
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                        int(hh), int(mm), 0, 0, 0, -1))


def add_task(profile: Profile, name: str, every: int, prompt: str,
             enabled: bool = True, session: str = "",
             kind: str = "interval", at: str = "") -> dict[str, Any]:
    """新增任务；重名拒绝（先 remove 再 add 才是改配置的正确路径）。

    kind="interval"：每 every 秒执行一次（既有行为）。
    kind="daily"：每天 at（"HH:MM"）执行一次，every 忽略。
    session 非空时，任务每次执行的结果会回写进该会话（结果回流）。
    """
    clean = validate_name(name)
    text = str(prompt or "").strip()
    if not text:
        raise ScheduleError("prompt 不能为空（到点要执行什么提示词？）")
    kind_clean = str(kind or "interval").strip().lower()
    task: dict[str, Any] = {"name": clean, "prompt": text,
                            "enabled": bool(enabled), "last_run": 0.0,
                            "created": time.time(),
                            "session": str(session or "").strip()}
    if kind_clean in ("daily", "clock", "每天"):
        task["kind"] = "daily"
        task["at"] = normalize_at(at)
    else:
        task["kind"] = "interval"
        task["every"] = validate_every(every)
    tasks = load_tasks(profile)
    if any(str(t.get("name")) == clean for t in tasks):
        raise ScheduleError(f"任务 '{clean}' 已存在（先 sha schedule remove {clean}）")
    tasks.append(task)
    save_tasks(profile, tasks)
    return task


def set_task_session(profile: Profile, name: str, session: str) -> bool:
    """设置/清除任务的结果回流会话（空串 = 只写日志不回流）。不存在返回 False。"""
    clean = validate_name(name)
    tasks = load_tasks(profile)
    hit = False
    for task in tasks:
        if str(task.get("name")) == clean:
            task["session"] = str(session or "").strip()
            hit = True
            break
    if hit:
        save_tasks(profile, tasks)
    return hit


def write_back_result(profile: Profile, task: dict[str, Any], content: str,
                      is_busy=None) -> dict[str, Any] | None:
    """把定时任务的执行结果回写进 task.session 指定的会话（结果回流）。

    会话文件追加一组 user/assistant 消息；``is_busy(session_id)`` 返回 True 时
    跳过——该会话正在流式回答，此刻落盘会被流结束时的整史覆盖。
    返回 {"session": sid} 或 None（未配置/跳过）。
    """
    target = str((task or {}).get("session") or "").strip()
    text = str(content or "").strip()
    if not target or not text:
        return None
    if callable(is_busy) and is_busy(target):
        return None
    history = profile.load_session(target)
    name = str((task or {}).get("name") or "定时任务")
    history.append({"role": "user",
                    "content": f"⏰ 定时任务「{name}」执行结果："})
    history.append({"role": "assistant", "content": text})
    profile.save_session(target, history)
    return {"session": target}


def remove_task(profile: Profile, name: str) -> bool:
    clean = validate_name(name)
    tasks = load_tasks(profile)
    remaining = [t for t in tasks if str(t.get("name")) != clean]
    if len(remaining) == len(tasks):
        return False
    save_tasks(profile, remaining)
    return True


def set_task_enabled(profile: Profile, name: str, enabled: bool) -> bool:
    """暂停 / 恢复单个任务（只翻 enabled 标志，不动 last_run）。

    任务不存在返回 False；存在则写入并返回 True。worker 每轮 tick 重读盘，
    因此对正在运行的 harness 立即生效。
    """
    clean = validate_name(name)
    tasks = load_tasks(profile)
    hit = False
    for task in tasks:
        if str(task.get("name")) == clean:
            task["enabled"] = bool(enabled)
            hit = True
            break
    if hit:
        save_tasks(profile, tasks)
    return hit


def due_tasks(tasks: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """挑出到点应执行的任务（enabled 且到达触发条件）。

    纯函数，方便离线测试调度判定而不必真起线程。

    - interval：now - last_run >= every（既有行为）
    - daily：now 已过今天的 at 时刻，且 last_run 早于该时刻（每天最多一次，
      错过（关机/停用）则下次运行时补跑一次）
    """
    due = []
    for task in tasks:
        if not task.get("enabled", True):
            continue
        last = task.get("last_run") or 0.0
        try:
            last = float(last)
        except (TypeError, ValueError):
            last = 0.0
        if str(task.get("kind") or "interval").lower() == "daily":
            try:
                target = daily_target_ts(str(task.get("at") or ""), now)
            except ScheduleError:
                continue  # at 写坏的任务跳过，不拖垮调度线程
            if now >= target and last < target:
                due.append(task)
            continue
        try:
            every = validate_every(task.get("every"))
        except ScheduleError:
            continue  # 配置写坏的任务跳过，不拖垮调度线程
        if now - last >= every:
            due.append(task)
    return due
