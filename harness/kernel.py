"""微内核：Harness 宿主与插件生命周期（对齐 DeepSeek Harness 的 Cordis 模型）。

设计要点（与 dsh 一一对应）：
    - 内核零业务逻辑：对话、模型、工具、技能、UI 全部由插件提供
    - Context 是插件与宿主交互的唯一边界：provide / on / effect
    - effect(fn) 注册的资源必须返回 disposer，deactivate 时按 LIFO 回滚、幂等
    - 插件状态机：PENDING → ACTIVE → FAILED / DISPOSED（FAILED 不拖垮整体）
    - 服务注册表：插件 provide 的服务按名字聚合，后激活者覆盖（同 dsh 服务语义）
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# 对齐 dsh 的 fiber 状态机
PENDING = "PENDING"
ACTIVE = "ACTIVE"
FAILED = "FAILED"
DISPOSED = "DISPOSED"


class EventBus:
    """极简事件总线：emit 逐个调用 handler，单个 handler 出错不影响其余。"""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = {}

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
            except Exception:
                # 但不能静默：否则插件监听器坏了宿主完全无感
                _logger.warning("事件 %r 的监听器 %r 执行失败", event, handler, exc_info=True)


# ----------------------------------------------------------------------
class _Tools:
    """ctx.tools：注册 agent 可调用的工具。"""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    def register(self, source: Any, name: str | None = None) -> Any:
        from nanoagent.tools import Tool, make_tool

        tool = source if isinstance(source, Tool) else make_tool(source, name=name)
        self._ctx.tools_list.append(tool)
        return tool


class _Skills:
    """ctx.skills：声明插件携带的 SKILL.md 技能目录。"""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    def add_dir(self, path: str | Path) -> None:
        path = str(path)
        if path not in self._ctx.skill_dirs:
            self._ctx.skill_dirs.append(path)


class _Models:
    """ctx.models：注册模型客户端（对齐 dsh 的模型适配器插件）。"""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    def register(self, name: str, llm: Any) -> None:
        self._ctx.models_map[name] = llm


class _Commands:
    """ctx.commands：注册 REPL 斜杠命令（UI 也是插件）。"""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    def register(self, name: str, handler: Callable, help_text: str = "") -> None:
        self._ctx.commands_map[name] = {"handler": handler, "help": help_text}


class Context:
    """插件上下文：与宿主交互的唯一边界（对齐 dsh 的 ctx）。

    ctx.host 指向 Harness 宿主（profile / confirm 回调等运行时设施挂在那里）。
    """

    def __init__(self, plugin_name: str, bus: EventBus, host: Harness | None = None) -> None:
        self.plugin_name = plugin_name
        self.bus = bus
        self.host = host
        self.services: dict[str, Any] = {}
        self.tools_list: list[Any] = []
        self.skill_dirs: list[str] = []
        self.models_map: dict[str, Any] = {}
        self.commands_map: dict[str, dict[str, Any]] = {}
        self.skipped: list[str] = []
        self._disposers: list[Callable] = []
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

    def effect(self, fn: Callable[[], Callable | None]) -> None:
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
    manifest: dict[str, Any] = field(default_factory=dict)
    ctx: Context | None = None
    provided: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    error: str = ""
    deps: list[str] = field(default_factory=list)
    # 装载期就已知的问题（清单损坏、依赖缺失等）。与 skipped 分开存：
    # activate() 会用 ctx.skipped 覆写 skipped，若把装载期的问题也塞在那里会被抹掉。
    mount_notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"name": self.name, "state": self.state, "provided": self.provided,
                "skipped": self.skipped, "error": self.error, "deps": self.deps}


# ----------------------------------------------------------------------
class Harness:
    """宿主：发现、激活、卸载插件；聚合它们提供的能力。"""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.plugins: dict[str, PluginRecord] = {}
        self.services: dict[str, Any] = {}
        self.service_conflicts: dict[str, list[str]] = {}  # 服务名 → 提供者列表
        # profile: Any 而非 Profile —— config↔kernel 会循环导入，运行时由 cli 注入
        self.profile: Any = None
        self.workspace: str = ""   # 由 cli 注入的绝对路径，工具与 shell 都假定它存在
        self.confirm: Callable[[str], bool] | None = None  # 权限确认回调（REPL 注入）

    # -- 发现 ------------------------------------------------------------
    def mount(self, path: str | Path, name: str | None = None) -> PluginRecord:
        """装载一个插件目录（不激活）。重名自动加序号后缀。"""
        from .loader import MANIFEST_ERROR_KEY, load_manifest, manifest_deps

        path = Path(path)
        manifest = load_manifest(path)
        final_name = name or manifest.get("name") or path.name
        base = final_name
        counter = 2
        while final_name in self.plugins:
            final_name = f"{base}-{counter}"
            counter += 1
        record = PluginRecord(name=final_name, path=str(path), manifest=manifest,
                              deps=manifest_deps(manifest))
        manifest_error = record.manifest.pop(MANIFEST_ERROR_KEY, "")
        if manifest_error:
            record.mount_notes.append(f"bad-manifest: {manifest_error}")
        self.plugins[final_name] = record
        return record

    def mount_all(self, directory: str | Path) -> builtins.list[PluginRecord]:
        """装载目录下全部插件子目录。"""
        root = Path(directory)
        discovered = []
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and ((child / "plugin.json").is_file() or (child / "register.py").is_file()):
                    discovered.append(self.mount(child))
        return discovered

    # -- 激活 / 卸载 ------------------------------------------------------
    def activation_order(self) -> builtins.list[str]:
        """按依赖关系排出激活顺序（拓扑排序）。

        未声明 ``inject`` 的插件保持挂载顺序（稳定）。此前顺序**只由目录名排序决定**，
        任何在 register() 里直接用 ``ctx.host.service(...)`` 的插件，
        只要名字排在被依赖者前面就会拿到 None —— 现在声明了依赖就不必赌字母序。

        依赖成环时不阻塞：记录一条 warning，环内成员按挂载顺序激活。
        """
        order: list[str] = []
        marks: dict[str, int] = {}   # 0=访问中，1=已完成
        cycles: list[str] = []

        def visit(name: str, stack: list[str]) -> None:
            state = marks.get(name)
            if state == 1:
                return
            if state == 0:
                cycles.append(" → ".join([*stack, name]))
                return
            marks[name] = 0
            for dep in self.plugins[name].deps:
                if dep in self.plugins:
                    visit(dep, [*stack, name])
            marks[name] = 1
            order.append(name)

        for name in list(self.plugins):
            visit(name, [])
        for chain in cycles:
            _logger.warning("插件依赖成环，环内成员按挂载顺序激活：%s", chain)
        return order

    def _dependency_notes(self, record: PluginRecord) -> builtins.list[str]:
        """检查声明的依赖是否真的可用，返回给用户看的问题列表。"""
        notes: list[str] = []
        for dep in record.deps:
            other = self.plugins.get(dep)
            if other is None:
                notes.append(f"missing-dep: 依赖的插件 {dep} 未装载")
            elif other.state != ACTIVE:
                notes.append(f"dep-not-active: 依赖的插件 {dep} 状态为 {other.state}")
        return notes

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
        # 必须清空：FAILED 插件修好后重新激活能走到这里，
        # 若留着旧 error，/status 会长期显示一个早已不存在的失败原因。
        record.error = ""
        record.provided = {
            "tools": [t.name for t in ctx.tools_list],
            "services": sorted(ctx.services),
            "models": sorted(ctx.models_map),
            "commands": sorted(ctx.commands_map),
        }
        # 装载期的问题 + 依赖问题 + 插件自报的 skip，三处合并（去重保序）
        notes = list(record.mount_notes) + self._dependency_notes(record) + list(ctx.skipped)
        record.skipped = list(dict.fromkeys(notes))
        self._rebuild_services()
        self.bus.emit("activate", name=record.name)
        return record

    def _rebuild_services(self) -> None:
        """从当前 ACTIVE 插件重建服务注册表（后挂载者覆盖）。

        模型池（ctx.models_map）也在这里重建。旧实现只在 activate() 里写模型池，
        deactivate() 重建服务表时漏掉这一段 —— 于是卸载任意插件后
        service("models") 都会凭空消失。

        同名服务被多个插件提供时会记 warning 并留档到 service_conflicts：
        「后者静默覆盖前者」是插件系统里最难查的一类问题，至少要让它可见。
        """
        services: dict[str, Any] = {}
        owners: dict[str, str] = {}
        conflicts: dict[str, list[str]] = {}
        models: dict[str, Any] = {}
        for plugin in self.plugins.values():
            if plugin.state != ACTIVE or plugin.ctx is None:
                continue
            for key, value in plugin.ctx.services.items():
                if key in services and owners.get(key) != plugin.name:
                    providers = conflicts.setdefault(key, [owners[key]])
                    if plugin.name not in providers:
                        providers.append(plugin.name)
                        _logger.warning("服务 %r 同时由 %s 与 %s 提供，后者覆盖前者",
                                        key, " 与 ".join(providers[:-1]), plugin.name)
                services[key] = value
                owners[key] = plugin.name
            for model_name, llm in plugin.ctx.models_map.items():
                models[model_name] = llm
        if models or "models" in services:
            services["models"] = models
        self.services = services
        self.service_conflicts = conflicts

    def activate_all(self) -> builtins.list[PluginRecord]:
        return [self.activate(name) for name in self.activation_order()]

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

    def reload(self, name: str) -> PluginRecord:
        """卸载并重新装载 + 激活单个插件（改完插件代码无需重启整个 harness）。

        loader 本来就在卸载时把合成模块从 sys.modules 摘掉，所以重新 execute
        拿到的一定是新代码 —— 缺的只是「让宿主把这件事串起来」。
        """
        record = self.plugins.get(name)
        if record is None:
            raise KeyError(f"插件 '{name}' 不存在")
        path = record.path
        self.deactivate(name)
        del self.plugins[name]
        fresh = self.mount(path, name=name)
        self.bus.emit("reload", name=name)
        return self.activate(fresh.name)

    # -- 能力聚合 --------------------------------------------------------
    def collect_tools(self) -> builtins.list[Any]:
        tools: list[Any] = []
        for record in self.plugins.values():
            if record.state == ACTIVE and record.ctx is not None:
                tools.extend(record.ctx.tools_list)
        return tools

    def collect_skill_dirs(self) -> builtins.list[str]:
        dirs: list[str] = []
        for record in self.plugins.values():
            if record.state == ACTIVE and record.ctx is not None:
                dirs.extend(record.ctx.skill_dirs)
        return dirs

    def collect_commands(self) -> dict[str, dict[str, Any]]:
        """聚合全部插件注册的 REPL 斜杠命令。"""
        commands: dict[str, dict[str, Any]] = {}
        for record in self.plugins.values():
            if record.state == ACTIVE and record.ctx is not None:
                commands.update(record.ctx.commands_map)
        return commands

    def service(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)

    def list(self) -> builtins.list[dict]:
        return [r.summary() for r in self.plugins.values()]
