"""REPL 插件：终端交互界面。UI 也走插件系统——可以整个换掉（如换成 Web 面板）。

本轮对齐桌面端（此前 CLI 与桌面端能力倒挂：桌面端有会话列表/用量/模型卡片，
CLI 连自己有哪些历史会话都看不到）：

- ``/sessions`` 列出历史会话，``/resume <id>`` 切回去，``/new`` 开新会话
- ``/usage`` 看本进程累计 token 用量
- ``/help``（此前 help 文本存在却没有命令入口，敲 /help 会得到「未知命令」）
- 启动时默认**续用最近一次会话**（与常见 CLI agent 一致），``sha chat --new`` 强制开新的
- 有 readline 时启用历史记录（存 profile/history.txt）与斜杠命令的 Tab 补全
"""

from __future__ import annotations

from pathlib import Path


def _latest_session(host) -> str:
    """最近改动过的会话 id；没有历史会话时回落 "default"。"""
    try:
        ids = host.profile.session_ids()
        if not ids:
            return "default"
        return max(ids, key=lambda sid: (host.profile.sessions_dir / f"{sid}.json").stat().st_mtime)
    except OSError:
        return "default"


def _install_readline(host, profile_root) -> None:
    """启用行编辑历史与 Tab 补全（Windows 无 readline 模块，静默跳过）。"""
    try:
        import readline
    except ImportError:
        return

    history_path = Path(profile_root) / "history.txt"
    # readline 的类型存根在 Windows 上是残缺的（pyreadline3 不带 .pyi），
    # 以下属性调用全部有 try/except 兜底，mypy 误报直接压掉。
    try:
        if history_path.is_file():
            readline.read_history_file(str(history_path))  # type: ignore[attr-defined]
        readline.set_history_length(500)  # type: ignore[attr-defined]
    except OSError:
        pass

    def save_history() -> None:
        try:
            readline.write_history_file(str(history_path))  # type: ignore[attr-defined]
        except OSError:
            pass

    import atexit

    atexit.register(save_history)

    candidates = ["/exit", "/new", "/help", "/sessions", "/resume", "/usage",
                  "/image", "/reload", "/plugins", "/status", "/approvals",
                  "/snip", "/plan"]
    candidates += [f"/{name}" for name in host.collect_commands()]

    def completer(text: str, state: int):
        options = [item for item in candidates if item.startswith(text)]
        return options[state] if state < len(options) else None

    try:
        readline.set_completer(completer)  # type: ignore[attr-defined]
        readline.parse_and_bind("tab: complete")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 —— 不同 readline 实现的怪癖不该让 REPL 起不来
        pass


def register(ctx) -> None:
    host = ctx.host

    def run_ui(once_message: str | None = None, session_id: str | None = None,
               force_new: bool = False) -> None:
        ask = host.service("ask")
        ask_stream = host.service("ask_stream")
        can_stream = host.service("can_stream")
        new_session = host.service("new_session")
        switch_session = host.service("switch_session")
        # 功能7：/image 附加的图片，随**下一轮**消息发出（用后即清，不污染后续轮次）
        pending_images: list[str] = []

        def add_image(raw: str) -> None:
            from harness.workspace import safe_image

            text = raw.strip().strip('"').strip("'")
            if not text:
                print("用法：/image <图片路径|URL>；/image clear 清空待发图片")
                return
            if text == "clear":
                pending_images.clear()
                print("已清空待发图片")
                return
            try:
                pending_images.append(safe_image(host, text))
            except (ValueError, OSError) as exc:
                print(f"[出错] {exc}")
                return
            print(f"已附加第 {len(pending_images)} 张图片（随下一条消息发送）")

        def streaming_ready() -> bool:
            """是否走流式：不支持就一次性，不做「先试再回退」（会重复工具副作用）。"""
            return callable(ask_stream) and callable(can_stream) and can_stream()

        def run_turn(text: str) -> None:
            """执行一轮对话并打印（支持则逐字输出，工具调用插在中间）。"""
            images = pending_images or None
            pending_images.clear()
            if not streaming_ready():
                print(ask(text, session_id, images=images)["reply"])
                return
            printed = False
            for event in ask_stream(text, session_id, images=images):
                kind = event.get("type")
                if kind == "delta":
                    print(event["text"], end="", flush=True)
                    printed = True
                elif kind == "tool_call":
                    if printed:
                        print()
                        printed = False
                    result = str(event.get("result") or "").replace("\n", " ")[:160]
                    print(f"  ⚙ {event.get('name')} → {result}")
            if printed:
                print()

        # 会话选择：显式 --session 优先；--new 强制新建；否则续用最近一次
        if force_new:
            session_id = new_session()
        elif session_id:
            session_id = switch_session(session_id)
        else:
            session_id = switch_session(_latest_session(host))

        def print_help() -> str:
            lines = [(
                "内置: /exit /new /help /sessions /resume <id> /usage /image <路径|URL> "
                "/reload <插件> /plugins /status /approvals /snip /plan"
            )]
            for name, cmd in sorted(host.collect_commands().items()):
                lines.append(f"  /{name}  {cmd.get('help', '')}")
            return "\n".join(lines)

        def list_sessions() -> None:
            current = host.service("current_session")
            current = current() if callable(current) else session_id
            ids = host.profile.session_ids()
            if not ids:
                print("（还没有历史会话）")
                return
            print("历史会话（* 为当前）:")
            for sid in ids:
                count = len(host.profile.load_session(sid))
                print(f" {'*' if sid == current else ' '} {sid}  ({count} 条消息)")

        def show_usage() -> None:
            factory = host.service("agent_factory")
            llm = getattr(factory(), "llm", None) if callable(factory) else None
            usage = dict(getattr(llm, "total_usage", None) or {})
            if not usage:
                print("（暂无用量记录）")
                return
            print("累计用量: " + "  ".join(f"{k}={v}" for k, v in usage.items()))

        def run_snip(args: str) -> bool:
            """/snip 快捷指令库。返回 True 表示已作为一轮对话发送（run_snip 内部处理）。"""
            from harness.snippets import snippets_delete, snippets_list, snippets_save

            name, _, rest = args.strip().partition(" ")
            if not name or name in ("list", "ls"):
                items = snippets_list(host.profile)
                if not items:
                    print("（还没有快捷指令。添加：/snip add <名称> <提示词内容>）")
                    return False
                print("快捷指令:")
                for entry in items:
                    first = entry["text"].split("\n")[0][:50]
                    print(f"  {entry['name']}  {first}")
                return False
            if name == "add":
                item_name, _, text = rest.strip().partition(" ")
                res = snippets_save(host.profile, item_name, text)
                print(res.get("error") or f"已保存快捷指令「{res.get('name')}」")
                return False
            if name == "del":
                if not rest.strip():
                    print("用法：/snip del <名称>")
                    return False
                res = snippets_delete(host.profile, rest.strip())
                print(res.get("error") or f"已删除快捷指令「{res.get('name')}」")
                return False
            # /snip <名称> → 把片段作为一轮对话直接发送
            item: dict[str, str] | None = None
            for entry in snippets_list(host.profile):
                if entry["name"] == name:
                    item = entry
                    break
            if item is None:
                print(f"没有快捷指令「{name}」（/snip 查看全部）")
                return False
            text = str(item.get("text") or "")
            print(f"▶ 发送快捷指令「{item.get('name')}」")
            run_turn(text)
            return True

        def run_command(line: str) -> None:
            nonlocal session_id, ask, new_session, switch_session
            name, _, args = line.partition(" ")
            if name == "/exit":
                raise SystemExit(0)
            if name == "/help":
                print(print_help())
                return
            if name == "/new":
                # new_session() 返回**新的会话 id**；不更新本地变量的话，
                # 下一轮仍会按旧 id 提问，等于没换会话。
                session_id = new_session()
                print(f"已开始新会话 {session_id}")
                return
            if name == "/sessions":
                list_sessions()
                return
            if name == "/resume":
                target = args.strip()
                if not target:
                    print("用法：/resume <会话id>（用 /sessions 查看）")
                    return
                if target not in host.profile.session_ids():
                    print(f"没有会话 {target}（用 /sessions 查看可用会话）")
                    return
                session_id = switch_session(target)
                print(f"已切到会话 {session_id}")
                return
            if name == "/usage":
                show_usage()
                return
            if name == "/image":
                add_image(args)
                return
            if name == "/snip":
                run_snip(args)
                return
            if name == "/plan":
                # 计划模式开关：/plan 切换；/plan on|off 显式设置
                setter = host.service("set_plan_mode")
                if not callable(setter):
                    print("错误：对话插件未激活")
                    return
                arg = args.strip().lower()
                current = bool(host.profile.load_config().get("plan_mode"))
                plan_on = (arg == "on") if arg in ("on", "off") else (not current)
                print(setter(plan_on))
                return
            if name == "/reload":
                target = args.strip()
                if not target:
                    print("用法：/reload <插件名>（用 /plugins 查看）")
                    return
                if target not in host.plugins:
                    print(f"没有插件 {target}（用 /plugins 查看）")
                    return
                loaded = host.reload(target)
                if loaded.state != "ACTIVE":
                    print(f"重载后激活失败: {loaded.error or loaded.state}")
                    return
                # 插件重载会换掉它提供的服务对象，本界面持有的旧引用必须换新，
                # 否则重载 chat_loop / repl 之后，对话还走在旧闭包上。
                ask = host.service("ask")
                new_session = host.service("new_session")
                switch_session = host.service("switch_session")
                current = host.service("current_session")
                if callable(current):
                    session_id = current()
                print(f"已重载 {loaded.name}")
                return
            if name == "/plugins":
                for plugin in host.list():
                    state = f"{plugin['name']}  [{plugin['state']}]"
                    provided = ", ".join(f"{k}={','.join(v)}" for k, v in plugin["provided"].items() if v)
                    print("  " + state + ("  " + provided if provided else "")
                          + (f"  错误: {plugin['error']}" if plugin["error"] else ""))
                return
            if name == "/status":
                runtime = host.service("models_runtime") or {}
                print(f"模型: {runtime.get('current', '?')}  会话: {session_id}")
                return
            commands = host.collect_commands()
            if name in commands:
                print(commands[name]["handler"](args.strip()))
                return
            print(f"未知命令 {name}。可用:\n{print_help()}")

        if once_message is not None:
            run_turn(once_message)
            return

        _install_readline(host, host.profile.root)
        mode = "流式" if streaming_ready() else "整段"
        print(f"卅 harness | 会话 {session_id} | {mode}输出 | /help 帮助 | /exit 退出")
        while True:
            try:
                user_input = input("\n你 > ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n再见！")
                return
            if not user_input:
                continue
            if user_input.startswith("/"):
                try:
                    run_command(user_input)
                except SystemExit:
                    print("再见！")
                    return
                except Exception as exc:  # noqa: BLE001
                    print(f"[出错] {exc}")
                continue
            try:
                run_turn(user_input)
            except Exception as exc:  # noqa: BLE001
                print(f"[出错] {type(exc).__name__}: {exc}")

    ctx.provide("ui", run_ui)
