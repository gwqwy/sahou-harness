"""REPL 插件：终端交互界面。UI 也走插件系统——可以整个换掉（如换成 Web 面板）。"""

from __future__ import annotations


def register(ctx) -> None:
    host = ctx.host

    def run_ui(once_message: str | None = None) -> None:
        ask = host.service("ask")
        new_session = host.service("new_session")
        session_id = "default"

        def print_help() -> str:
            lines = ["内置: /exit /new /plugins /status"]
            for name, cmd in sorted(host.collect_commands().items()):
                lines.append(f"  /{name}  {cmd.get('help', '')}")
            return "\n".join(lines)

        def run_command(line: str) -> None:
            name, _, args = line.partition(" ")
            if name == "/exit":
                raise SystemExit(0)
            if name == "/new":
                print(new_session())
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
            result = ask(once_message, session_id)
            print(result["reply"])
            return

        print("卅 harness | /exit 退出 | /plugins 插件 | /model 模型 | /new 新会话")
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
                result = ask(user_input, session_id)
                print(result["reply"])
            except Exception as exc:  # noqa: BLE001
                print(f"[出错] {type(exc).__name__}: {exc}")

    ctx.provide("ui", run_ui)
