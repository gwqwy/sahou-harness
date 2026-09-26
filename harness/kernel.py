"""微内核：Harness 宿主与插件生命周期（对齐 DeepSeek Harness 的 Cordis 模型）。

设计要点（与 dsh 一一对应）：
    - 内核零业务逻辑：对话、模型、工具、技能、UI 全部由插件提供
    - Context 是插件与宿主交互的唯一边界：provide / on / effect
    - effect(fn) 注册的资源必须返回 disposer，deactivate 时按 LIFO 回滚、幂等
    - 插件状态机：PENDING → ACTIVE → FAILED / DISPOSED（FAILED 不拖垮整体）
    - 服务注册表：插件 provide 的服务按名字聚合，后激活者覆盖（同 dsh 服务语义）
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_logger = logging.getLogger(__name__)

# 对齐 dsh 的 fiber 状态机
PENDING = "PENDING"
ACTIVE = "ACTIVE"
FAILED = "FAILED"
DISPOSED = "DISPOSED"


class EventBus:
    """极简事件总线：emit 逐个调用 handler，单个 handler 出错不影响其余。"""

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Callable]] = {}

    def on(self, event: str, handler: Callable) -> Callable:
        """订阅事件，返回取消订阅的 disposer。"""
        self._handlers.setdefault(event, []).append(handler)

        def dispose() -> None:
            handlers = self._handlers.get(event, [])
            if handler in handlers:
                handlers.remove(handler)

        return dispose

    def emit(self, event: str, **payload: Any) -> None:
        for handler in list(self._handlers.get(event, [])):
            try:
                handler(**payload)
            except Exception:  # noqa: BLE001 —— 观察者出错不阻断宿主
                # 但不能静默：否则插件监听器坏了宿主完全无感
                _logger.warning("事件 %r 的监听器 %r 执行失败", event, handler, exc_info=True)


# ----------------------------------------------------------------------
class _Tools:
    """ctx.tools：注册 agent 可调用的工具。"""

    def __init__(self, ctx: "Context") -> None:
        self._ctx = ctx

    def register(self, source: Any, name: Optional[str] = None) -> Any:
        from nanoagent.tools import Tool, make_tool

        tool = source if isinstance(source, Tool) else make_tool(source, name=name)
        self._ctx.tools_list.append(tool)
        return tool


class _Skills:
    """ctx.skills：声明插件携带的 SKILL.md 技能目录。"""

    def __init__(self, ctx: "Context") -> None:
        self._ctx = ctx

    def add_dir(self, path: str | Path) -> None:
        path = str(path)
        if path not in self._ctx.skill_dirs:
            self._ctx.skill_dirs.append(path)


class _Models:
    """ctx.models：注册模型客户端（对齐 dsh 的模型适配器插件）。"""

    def __init__(self, ctx: "Context") -> None:
        self._ctx = ctx

    def register(self, name: str, llm: Any) -> None:
        self._ctx.models_map[name] = llm


class _Commands:
    """ctx.commands：注册 REPL 斜杠命令（UI 也是插件）。"""

    def __init__(self, ctx: "Context") -> None:
        self._ctx = ctx

    def register(self, name: str, handler: Callable, help_text: str = "") -> None:
        self._ctx.commands_map[name] = {"handler": handler, "help": help_text}


class Context:
    """插件上下文：与宿主交互的唯一边界（对齐 dsh 的 ctx）。

    ctx.host 指向 Harness 宿主（profile / confirm 回调等运行时设施挂在那里）。
    """

    def __init__(self, plugin_name: str, bus: EventBus, host: Optional["Harness"] = None) -> None:
        self.plugin_name = plugin_name
        self.bus = bus
        self.host = host
        self.services: Dict[str, Any] = {}
        self.tools_list: List[Any] = []
        self.skill_dirs: List[str] = []
        self.models_map: Dict[str, Any] = {}
        self.commands_map: Dict[str, Dict[str, Any]] = {}
        self.skipped: List[str] = []
        self._disposers: List[Callable] = []
        self.tools = _Tools(self)
        self.skills = _Skills(self)
        self.models = _Models(self)
        self.commands = _Commands(self)

    # -- 核心三原语 ------------------------------------------------------
    def provide(self, name: str, service: Any) -> Callable:
        """提供一个服务，返回撤销注册的 disposer。"""
        self.services[name] = service

        def dispose() -> None:
            self.services.pop(name, None)

        self._disposers.append(dispose)
        return dispose

    def on(self, event: str, handler: Callable) -> Callable:
        """订阅宿主事件（activate/deactivate/error），返回 disposer。

        注意：disposer **必须入账本**，否则插件卸载后监听器仍驻留在事件总线上
        （幽灵监听器），反复装卸会持续累积。
        """
        dispose = self.bus.on(event, handler)
        self._disposers.append(dispose)
        return dispose

    def effect(self, fn: Callable[[], Optional[Callable]]) -> None:
        """执行 fn 并把其返回的 disposer 记入账本（dsh 的 ctx.effect）。"""
        disposer = fn()
        if disposer is not None:
            self._disposers.append(disposer)

    # -- 生命周期 --------------------------------------------------------
    def deactivate(self) -> None:
        """LIFO 回滚全部 disposer；幂等。"""
        while self._disposers:
            disposer = self._disposers.pop()
            try:
                disposer()
            except Exception:  # noqa: BLE001
                pass


# ----------------------------------------------------------------------
@dataclass
class PluginRecord:
    """一个被发现的插件（对齐 dsh 的 fiber）。"""

    name: str
    path: str
    state: str = PENDING
    manifest: Dict[str, Any] = field(default_factory=dict)
    ctx: Optional[Context] = None
    provided: Dict[str, List[str]] = field(default_factory=dict)
    skipped: List[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> dict:
        return {"name": self.name, "state": self.state, "provided": self.provided,
                "skipped": self.skipped, "error": self.error}


# ----------------------------------------------------------------------
class Harness:
    """宿主：发现、激活、卸载插件；聚合它们提供的能力。"""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.plugins: Dict[str, PluginRecord] = {}
        self.services: Dict[str, Any] = {}
        self.profile = None        # 由运行时（cli）注入
        self.confirm: Optional[Callable[[str], bool]] = None  # 权限确认回调（REPL 注入）

    # -- 发现 ------------------------------------------------------------
    def mount(self, path: str | Path, name: Optional[str] = None) -> PluginRecord:
        """装载一个插件目录（不激活）。重名自动加序号后缀。"""
        from .loader import MANIFEST_ERROR_KEY, load_manifest

        path = Path(path)
        manifest = load_manifest(path)
        final_name = name or manifest.get("name") or path.name
        base = final_name
        counter = 2
        while final_name in self.plugins:
            final_name = f"{base}-{counter}"
            counter += 1
        record = PluginRecord(name=final_name, path=str(path), manifest=manifest)
        manifest_error = record.manifest.pop(MANIFEST_ERROR_KEY, "")
        if manifest_error:
            record.skipped.append(f"bad-manifest: {manifest_error}")
        self.plugins[final_name] = record
        return record

    def mount_all(self, directory: str | Path) -> List[PluginRecord]:
        """装载目录下全部插件子目录。"""
        root = Path(directory)
        discovered = []
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and ((child / "plugin.json").is_file() or (child / "register.py").is_file()):
                    discovered.append(self.mount(child))
        return discovered

    # -- 激活 / 卸载 ------------------------------------------------------
    def activate(self, name: str) -> PluginRecord:
        """激活插件：建 ctx、执行 register(ctx)、聚合提供物。失败不外抛。"""
        from .loader import run_register

        record = self.plugins.get(name)
        if record is None:
            raise KeyError(f"插件 '{name}' 不存在")
        if record.state == ACTIVE:
            return record

        ctx = Context(record.name, self.bus, host=self)
        try:
            run_register(record, ctx)
        except Exception as exc:  # noqa: BLE001 —— 单插件失败不拖垮整体
            ctx.deactivate()
            record.state = FAILED
            record.ctx = None
            record.error = f"{type(exc).__name__}: {exc}"
            self.bus.emit("error", name=record.name, error=record.error)
            return record

        record.ctx = ctx
        record.state = ACTIVE
        record.provided = {
            "tools": [t.name for t in ctx.tools_list],
            "services": sorted(ctx.services),
            "models": sorted(ctx.models_map),
            "commands": sorted(ctx.commands_map),
        }
        record.skipped = list(ctx.skipped)
        self._rebuild_services()
        self.bus.emit("activate", name=record.name)
        return record

    def _rebuild_services(self) -> None:
        """从当前 ACTIVE 插件重建服务注册表（后激活者覆盖）。

        模型池（ctx.models_map）也在这里重建。旧实现只在 activate() 里写模型池，
        deactivate() 重建服务表时漏掉这一段 —— 于是卸载任意插件后
        service("models") 都会凭空消失。
        """
        services: Dict[str, Any] = {}
        models: Dict[str, Any] = {}
        for plugin in self.plugins.values():
            if plugin.state == ACTIVE and plugin.ctx is not None:
                services.update(plugin.ctx.services)
                for model_name, llm in plugin.ctx.models_map.items():
                    models[model_name] = llm
        if models or "models" in services:
            services["models"] = models
        self.services = services

    def activate_all(self) -> List[PluginRecord]:
        return [self.activate(n) for n in list(self.plugins)]

    def deactivate(self, name: str) -> PluginRecord:
        record = self.plugins.get(name)
        if record is None:
            raise KeyError(f"插件 '{name}' 不存在")
        if record.ctx is not None:
            record.ctx.deactivate()
            record.ctx = None
        record.state = DISPOSED
        record.provided = {}
        self._rebuild_services()
        self.bus.emit("deactivate", name=record.name)
        return record

    # -- 能力聚合 --------------------------------------------------------
    def collect_tools(self) -> List[Any]:
        tools: List[Any] = []
        for record in self.plugins.values():
            if record.state == ACTIVE and record.ctx is not None:
                tools.extend(record.ctx.tools_list)
        return tools

    def collect_skill_dirs(self) -> List[str]:
        dirs: List[str] = []
        for record in self.plugins.values():
            if record.state == ACTIVE and record.ctx is not None:
                dirs.extend(record.ctx.skill_dirs)
        return dirs

    def collect_commands(self) -> Dict[str, Dict[str, Any]]:
        """聚合全部插件注册的 REPL 斜杠命令。"""
        commands: Dict[str, Dict[str, Any]] = {}
        for record in self.plugins.values():
            if record.state == ACTIVE and record.ctx is not None:
                commands.update(record.ctx.commands_map)
        return commands

    def service(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)

    def list(self) -> List[dict]:
        return [r.summary() for r in self.plugins.values()]
