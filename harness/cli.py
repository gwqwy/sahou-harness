"""卅 harness 命令行入口。

用法（python -m harness，或安装后用 sha 命令）：
    sha                            # 交互式 REPL（等价 sha chat）
    sha chat -m "一句话任务"        # 单次执行模式（测试/脚本友好）
    sha init                       # 初始化 profile（生成 config.json）
    sha plugin add <目录>          # 安装外部插件到 profile
    sha plugin list | remove <名>  # 查看 / 移除插件
    sha model list | use <名>      # 查看 / 切换模型
    sha skill list                 # 列出技能
    sha status                     # 插件与能力总览
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, Profile
from .kernel import Harness

BUILTINS_DIR = Path(__file__).resolve().parent / "builtins"

INSTRUCTIONS_EXTRA = ""


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
    parser.add_argument("--profile", default="default", help="profile 名称（默认 default）")
    parser.add_argument("--workspace", default=".", help="工作区目录（默认当前目录）")
    parser.add_argument("--home", default=None, help="harness 数据根目录（默认 ~/.sahou-harness）")
    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser("chat", help="交互式对话（默认命令）")
    chat.add_argument("-m", "--message", default=None, help="单次执行模式：执行该消息后退出")

    sub.add_parser("desktop", help="桌面端：pywebview 原生窗口（需 pip install pywebview）")

    sub.add_parser("init", help="初始化 profile")
    sub.add_parser("status", help="插件与能力总览")

    plugin = sub.add_parser("plugin", help="插件管理")
    plugin_sub = plugin.add_subparsers(dest="plugin_command", required=True)
    p_add = plugin_sub.add_parser("add", help="安装插件（本地目录）")
    p_add.add_argument("source")
    plugin_sub.add_parser("list", help="列出已安装插件")
    p_rm = plugin_sub.add_parser("remove", help="移除插件")
    p_rm.add_argument("name")

    model = sub.add_parser("model", help="模型管理")
    model_sub = model.add_subparsers(dest="model_command", required=True)
    model_sub.add_parser("list", help="列出模型")
    m_use = model_sub.add_parser("use", help="切换默认模型")
    m_use.add_argument("name")

    skill = sub.add_parser("skill", help="技能管理")
    skill_sub = skill.add_subparsers(dest="skill_command", required=True)
    skill_sub.add_parser("list", help="列出技能")
    return parser


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
                dest = profile.install_plugin(args.source)
                print(f"已安装插件到: {dest}")
            elif args.plugin_command == "list":
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

    # 以下命令需要激活运行时
    try:
        host = build_runtime(profile, args.workspace)
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if command == "model":
        runtime = host.service("models_runtime") or {"order": [], "current": ""}
        if args.model_command == "list":
            print("可用模型: " + ", ".join(runtime["order"]) + f"（当前: {runtime['current']}）")
        elif args.model_command == "use":
            order = list(runtime.get("order") or [])
            if order and args.name not in order:
                print(f"错误：模型不存在: {args.name}（可用: {', '.join(order)}）", file=sys.stderr)
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
            if plugin["skipped"]:
                line += f"  (skipped: {'; '.join(plugin['skipped'])})"
            if plugin["error"]:
                line += f"  错误: {plugin['error']}"
            print(line)
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
        ui(args.message)  # None → 交互式；字符串 → 单次执行
    except KeyboardInterrupt:
        print("\n再见！")
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 不把裸异常栈丢给用户（如密钥无效的 401）
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
