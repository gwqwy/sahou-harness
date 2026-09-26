"""卅 harness 命令行入口。

用法（python -m harness，或安装后用 sha 命令）：
    sha                            # 交互式 REPL（默认命令；续用最近一次会话）
    sha chat -m "一句话任务"        # 单次执行模式（测试/脚本友好）
    sha chat -s <会话id> -m "..."  # 指定会话
    sha chat --new                 # 强制开新会话
    sha sessions                   # 列出历史会话
    sha trace                      # 查看最近一次对话的 trace 摘要
    sha init                       # 初始化 profile（生成 config.json）
    sha plugin add <目录|git地址>   # 安装外部插件（装完立即校验能否激活）
    sha plugin list | remove <名>  # 查看 / 移除插件
    sha plugin new <名>            # 生成一个插件模板
    sha plugin reload <名>         # 重载单个插件（改完插件代码不必重启）
    sha model list | use <名>      # 查看 / 切换模型
    sha model add | remove <名>    # 增删模型（此前只能手改 config.json）
    sha skill list                 # 列出技能
    sha status                     # 插件与能力总览
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .config import ConfigError, Profile
from .kernel import ACTIVE, Harness

BUILTINS_DIR = Path(__file__).resolve().parent / "builtins"

INSTRUCTIONS_EXTRA = ""

PLUGIN_TEMPLATE_REGISTER = '''"""__NAME__ 插件：一句话说明它提供什么能力。"""

from __future__ import annotations


def register(ctx) -> None:
    host = ctx.host

    def hello(text: str) -> str:
        """打个招呼，把文本原样返回。

        Args:
            text: 任意文本
        """
        return f"你好，{text}"

    ctx.tools.register(hello)

    # ctx.provide("my.service", object())     # 提供服务，供其它插件消费
    # ctx.effect(lambda: 清理函数)             # 需要回滚的资源：返回 disposer，卸载时 LIFO 回滚
    # ctx.commands.register("hello", lambda args: "你好", "打招呼命令")   # REPL 斜杠命令
'''


def build_runtime(profile: Profile, workspace: str) -> Harness:
    """组装宿主：内核 + 内置插件 + profile 插件，全部激活。"""
    host = Harness()
    host.profile = profile
    # 立即规范化为绝对路径：内核各处（工作区树、shell cwd、工具沙箱）都假定它是绝对路径
    host.workspace = str(Path(workspace or ".").resolve())

    host.mount_all(BUILTINS_DIR)   # 内置插件（对话循环/模型/工具/技能/MCP/REPL）
    host.mount_all(profile.plugins_dir)  # 用户安装的外部插件
    host.activate_all()
    return host


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sha", description="卅 harness —— 一切皆插件的 agent harness")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--profile", default="default", help="profile 名称（默认 default）")
    parser.add_argument("--workspace", default=".", help="工作区目录（默认当前目录）")
    parser.add_argument("--home", default=None, help="harness 数据根目录（默认 ~/.sahou-harness）")
    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser("chat", help="交互式对话（默认命令）")
    chat.add_argument("-m", "--message", default=None, help="单次执行模式：执行该消息后退出")
    chat.add_argument("-s", "--session", default=None, help="会话 id（默认续用最近一次会话）")
    chat.add_argument("-n", "--new", action="store_true", help="强制开一个新会话")

    sub.add_parser("desktop", help="桌面端：pywebview 原生窗口（需 pip install pywebview）")

    sub.add_parser("init", help="初始化 profile")
    sub.add_parser("status", help="插件与能力总览")
    sub.add_parser("sessions", help="列出历史会话")
    sub.add_parser("trace", help="查看最近一次对话的 trace 摘要（需 tracing 插件）")

    plugin = sub.add_parser("plugin", help="插件管理")
    plugin_sub = plugin.add_subparsers(dest="plugin_command", required=True)
    p_add = plugin_sub.add_parser("add", help="安装插件（本地目录或 git/http 地址）")
    p_add.add_argument("source")
    plugin_sub.add_parser("list", help="列出已安装插件")
    p_rm = plugin_sub.add_parser("remove", help="移除插件")
    p_rm.add_argument("name")
    p_new = plugin_sub.add_parser("new", help="生成一个插件模板（plugin.json + register.py）")
    p_new.add_argument("name")
    p_new.add_argument("--dir", default=".", help="生成到哪个目录（默认当前目录）")
    p_reload = plugin_sub.add_parser("reload", help="重载单个插件（改完插件代码无需重启）")
    p_reload.add_argument("name")

    model = sub.add_parser("model", help="模型管理")
    model_sub = model.add_subparsers(dest="model_command", required=True)
    model_sub.add_parser("list", help="列出模型")
    m_use = model_sub.add_parser("use", help="切换默认模型")
    m_use.add_argument("name")
    m_add = model_sub.add_parser("add", help="添加模型")
    m_add.add_argument("name", help="模型在 harness 里的名字（唯一）")
    m_add.add_argument("--model", required=True, help="模型 ID，如 deepseek-chat")
    m_add.add_argument("--provider", default="openai", choices=["openai", "anthropic"])
    m_add.add_argument("--base-url", default=None, help="自定义 API 地址（可选）")
    m_add.add_argument("--api-key", default=None,
                       help="API Key；写成 ${ENV_VAR} 可只存环境变量名，不落明文")
    m_add.add_argument("--default", action="store_true", help="同时设为默认模型")
    m_rm = model_sub.add_parser("remove", help="删除模型")
    m_rm.add_argument("name")

    skill = sub.add_parser("skill", help="技能管理")
    skill_sub = skill.add_subparsers(dest="skill_command", required=True)
    skill_sub.add_parser("list", help="列出技能")
    return parser


def _plugin_new(name: str, directory: str) -> int:
    """生成插件模板目录，省掉「照抄一个现有插件」的步骤。"""
    safe_name = name.strip()
    if not safe_name or any(ch in safe_name for ch in '\\/:*?"<>|'):
        print(f"错误：插件名不合法: {name!r}", file=sys.stderr)
        return 1
    target = Path(directory).expanduser().resolve() / safe_name
    if target.exists():
        print(f"错误：目录已存在: {target}", file=sys.stderr)
        return 1
    try:
        target.mkdir(parents=True)
        (target / "plugin.json").write_text(
            json.dumps({"name": safe_name, "description": f"{safe_name} 插件：一句话说明它提供什么能力"},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (target / "register.py").write_text(
            # 不能用 str.format：模板里的 docstring 含 {text} 等字面花括号
            PLUGIN_TEMPLATE_REGISTER.replace("__NAME__", safe_name), encoding="utf-8")
    except OSError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"已生成插件模板: {target}")
    print("  下一步：填好 register.py，然后 `sha plugin add "
          f"\"{target}\"`（装完会立即校验能否激活）")
    return 0


def _plugin_add(profile: Profile, source: str, workspace: str) -> int:
    """安装插件，并立即尝试激活 —— 装错了当场就能知道，不必重启再猜。"""
    try:
        dest = profile.install_plugin(source)
    except (FileNotFoundError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"已安装到: {dest}")

    try:
        host = build_runtime(profile, workspace)
    except ConfigError as exc:
        print(f"注意：运行时构建失败（{exc}），跳过激活校验", file=sys.stderr)
        return 0
    target = None
    for record in host.plugins.values():
        try:
            if Path(record.path).resolve() == Path(dest).resolve():
                target = record
                break
        except OSError:
            continue
    if target is None:
        print("注意：未发现 plugin.json 或 register.py，该目录不会被当作插件装载", file=sys.stderr)
        return 1
    if target.state == ACTIVE:
        provided = ", ".join(f"{k}={','.join(v)}" for k, v in target.provided.items() if v)
        print(f"已激活: {target.name}" + (f"  {provided}" if provided else ""))
        return 0
    print(f"已复制，但激活失败: {target.error or target.state}（修好后重启即生效）", file=sys.stderr)
    return 1


def _plugin_reload(profile: Profile, workspace: str, name: str) -> int:
    """重载单个插件：卸载 → 重新装载 → 激活（改完插件代码不必重启整个 harness）。"""
    try:
        host = build_runtime(profile, workspace)
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if name not in host.plugins:
        print(f"错误：没有插件 '{name}'（可用: {', '.join(sorted(host.plugins))}）", file=sys.stderr)
        return 1
    record = host.reload(name)
    if record.state == ACTIVE:
        provided = ", ".join(f"{k}={','.join(v)}" for k, v in record.provided.items() if v)
        print(f"已重载: {record.name}" + (f"  {provided}" if provided else ""))
        return 0
    print(f"重载后激活失败: {record.error or record.state}", file=sys.stderr)
    return 1


def _model_add(profile: Profile, args) -> int:
    entry = {"name": args.name, "provider": args.provider, "model": args.model}
    if args.base_url:
        entry["base_url"] = args.base_url
    if args.api_key:
        entry["api_key"] = args.api_key
    problem: list[str] = []

    def mutate(config: dict) -> None:
        models = list(config.get("models") or [])
        if any(str(item.get("name")) == args.name for item in models):
            problem.append(f"模型 '{args.name}' 已存在（先 `sha model remove {args.name}`）")
            return
        models.append(entry)
        config["models"] = models
        if args.default or not config.get("default_model"):
            config["default_model"] = args.name

    profile.mutate_config(mutate)
    if problem:
        print(f"错误：{problem[0]}", file=sys.stderr)
        return 1
    print(f"已添加模型 {args.name}（{args.provider}/{args.model}）")
    if not args.api_key:
        default_var = "ANTHROPIC_API_KEY" if args.provider == "anthropic" else "OPENAI_API_KEY"
        print(f"  未给 api_key：运行时会尝试读取环境变量 {default_var}，"
              f"也可用 `--api-key ${default_var}` 只存变量名")
    print(f"  配置文件: {profile.config_path}")
    return 0


def _model_remove(profile: Profile, name: str) -> int:
    problem: list[str] = []

    def mutate(config: dict) -> None:
        models = list(config.get("models") or [])
        remaining = [item for item in models if str(item.get("name")) != name]
        if len(remaining) == len(models):
            problem.append(f"没有模型 '{name}'")
            return
        config["models"] = remaining
        if config.get("default_model") == name:
            config["default_model"] = str(remaining[0].get("name")) if remaining else ""
            problem.append(f"它原本是默认模型，已改为 "
                           f"{config['default_model'] or '（空，请用 sha model use 指定）'}")

    profile.mutate_config(mutate)
    if problem and problem[0].startswith("没有模型"):
        print(f"错误：{problem[0]}", file=sys.stderr)
        return 1
    print(f"已删除模型 {name}")
    for note in problem:
        print(f"  注意：{note}")
    return 0


def _list_sessions(profile: Profile) -> int:
    ids = profile.session_ids()
    if not ids:
        print("（还没有历史会话）")
        return 0
    rows = []
    for sid in ids:
        try:
            stat = (profile.sessions_dir / f"{sid}.json").stat()
        except OSError:
            continue
        rows.append((stat.st_mtime, sid, len(profile.load_session(sid))))
    rows.sort(reverse=True)
    print("历史会话（最新在上）:")
    for mtime, sid, count in rows:
        print(f"  {sid}  {count} 条消息  {time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))}")
    return 0


def main(argv: list | None = None) -> int:
    args = _make_parser().parse_args(argv)

    home = Path(args.home) if args.home else Path.home() / ".sahou-harness"
    try:
        profile = Profile(home / "profiles" / args.profile)
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    command = args.command or "chat"

    if command == "init":
        print(f"profile 已就绪: {profile.root}")
        print(f"配置文件: {profile.config_path}（编辑 models 填入你的模型）")
        return 0

    if command == "plugin":
        try:
            if args.plugin_command == "add":
                return _plugin_add(profile, args.source, args.workspace)
            if args.plugin_command == "new":
                return _plugin_new(args.name, args.dir)
            if args.plugin_command == "reload":
                return _plugin_reload(profile, args.workspace, args.name)
            if args.plugin_command == "list":
                installed = sorted(p.name for p in profile.plugins_dir.iterdir() if p.is_dir())
                print("\n".join(installed) or "（无）")
            elif args.plugin_command == "remove":
                if profile.remove_plugin(args.name):
                    print(f"已移除: {args.name}")
                else:
                    print(f"未找到可移除的插件目录: {args.name}", file=sys.stderr)
                    return 1
        except (FileNotFoundError, OSError) as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        return 0

    # 增删模型只改配置，不需要运行时 —— 配置写坏时也能用它自救
    if command == "model":
        if args.model_command == "add":
            return _model_add(profile, args)
        if args.model_command == "remove":
            return _model_remove(profile, args.name)

    if command == "sessions":
        return _list_sessions(profile)

    # 以下命令需要激活运行时
    try:
        host = build_runtime(profile, args.workspace)
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    # 退出时回收插件资源（H-06）：MCP 连接、追踪/审计文件句柄等都挂在插件的
    # effect disposer 上。此前进程退出从不调 deactivate，全靠 OS 兜底。
    # atexit 覆盖全部 return 路径（含 Ctrl+C 与异常）；shutdown 幂等，重复调用无害。
    import atexit

    atexit.register(host.shutdown)

    if command == "model":
        runtime = host.service("models_runtime") or {"order": [], "current": ""}
        if args.model_command == "list":
            print("可用模型: " + (", ".join(runtime["order"]) or "（无）")
                  + f"（当前: {runtime['current'] or '（未设置）'}）")
            for name, reason in sorted((runtime.get("errors") or {}).items()):
                print(f"  [不可用] {name}: {reason}")
        elif args.model_command == "use":
            order = list(runtime.get("order") or [])
            # 无条件校验（H-08b）：旧写法 `if order and ...` 在 models 全部不可用
            # （order 为空）时短路旁路，坏模型名仍会被持久化成 default_model。
            if args.name not in order:
                hint = f"（可用: {', '.join(order)}）" if order else "（当前没有可用模型，请先在 config.json 修正模型配置）"
                print(f"错误：模型不存在: {args.name}{hint}", file=sys.stderr)
                return 1
            print(runtime["set"](args.name) if "set" in runtime else
                  f"已写入配置: default_model={args.name}（重启后生效）")
            profile.update_config(default_model=args.name)
        return 0

    if command == "skill":
        from nanoagent.skills import SkillRegistry

        registry = SkillRegistry()
        for skill_dir in host.collect_skill_dirs():
            registry.add_dir(skill_dir)
        print(registry.list_summary() or "（无技能）")
        return 0

    if command == "status":
        for plugin in host.list():
            provided = ", ".join(f"{k}={','.join(v)}" for k, v in plugin["provided"].items() if v)
            line = f"  {plugin['name']}  [{plugin['state']}]  {provided}".rstrip()
            if plugin.get("deps"):
                line += f"  inject={','.join(plugin['deps'])}"
            if plugin["skipped"]:
                line += f"  (skipped: {'; '.join(plugin['skipped'])})"
            if plugin["error"]:
                line += f"  错误: {plugin['error']}"
            print(line)
        for service_name, providers in sorted(host.service_conflicts.items()):
            print(f"  冲突: 服务 {service_name} 由 {', '.join(providers)} 同时提供（后者覆盖）")
        return 0

    if command == "trace":
        tracing = host.service("tracing")
        if not tracing:
            print("（tracing 插件未启用，或 tracing.enabled=false）")
            return 0
        files = sorted(Path(tracing["dir"]).glob("*.jsonl"))
        if not files:
            print(f"暂无 trace（目录: {tracing['dir']}）")
            return 0
        from nanoagent.observability import trace_summary

        latest = files[-1]
        try:
            summary = trace_summary(latest)
        except Exception as exc:  # noqa: BLE001
            print(f"错误：无法读取 trace（{type(exc).__name__}: {exc}）", file=sys.stderr)
            return 1
        print(f"最新 trace: {latest.name}")
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str)
              if isinstance(summary, (dict, list)) else summary)
        return 0

    if command == "desktop":
        from .desktop import run as run_desktop

        return run_desktop(host)

    # chat（默认）
    ui = host.service("ui")
    if ui is None:
        print("错误：repl 插件未激活，没有可用界面", file=sys.stderr)
        return 1
    try:
        ui(once_message=args.message, session_id=args.session, force_new=args.new)
    except KeyboardInterrupt:
        print("\n再见！")
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 —— CLI 顶层兜底，不把裸异常栈丢给用户（如密钥无效的 401）
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
