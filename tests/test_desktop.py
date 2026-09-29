"""桌面端测试：进程内 API（chat/思考/上下文/工作区/会话命名/权限），不依赖窗口。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_harness import allow_fs, build_host, make_profile, patch_model_build

TESTS_DIR = Path(__file__).resolve().parent


class FakeWindow:
    """假窗口：模拟 evaluate_js 驱动的对话框轮询。"""

    def __init__(self, answers):
        self.calls = []
        self._answers = list(answers)  # 每次读取 __dialogResult 时依次弹出

    def evaluate_js(self, js):
        self.calls.append(js)
        if js.strip() == "window.__dialogResult" and self._answers:
            return self._answers.pop(0)
        return None


class DesktopEntrypointTests(unittest.TestCase):
    """批次F：desktop.run 的启动路径（不需要真窗口）。"""

    def test_run_reports_corrupt_config_instead_of_crashing(self):
        """config.json 损坏时应打印可读错误并返回 2。

        回归：该分支用了 ``sys.stderr`` 却没 ``import sys``，真实效果是 NameError
        把 ConfigError 盖掉 —— 用户看到一句跟配置毫无关系的报错，而真正的
        「原文件已备份为 xxx，请修复后再启动」根本没机会显示。
        """
        import contextlib
        import io
        import types

        with tempfile.TemporaryDirectory() as tmp:
            from harness.desktop import run

            with patch_model_build():
                host = build_host(Path(tmp))
            host.profile.config_path.write_text("{ 这不是合法 json", encoding="utf-8")

            # 把 pywebview 换成一个什么都不做的假模块，绕开「需要装界面库」的早退
            fake = types.ModuleType("webview")
            fake.create_window = lambda *a, **k: object()
            fake.start = lambda *a, **k: None
            saved = sys.modules.get("webview")
            sys.modules["webview"] = fake
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    code = run(host)
            finally:
                if saved is None:
                    sys.modules.pop("webview", None)
                else:
                    sys.modules["webview"] = saved

            self.assertEqual(code, 2)
            self.assertIn("错误", stderr.getvalue())
            self.assertIn("config.json", stderr.getvalue())


class DesktopApiTests(unittest.TestCase):
    def _app(self, tmp: Path):
        from harness.desktop import DesktopApp

        with patch_model_build():
            host = build_host(tmp)
        return host, DesktopApp(host)

    def test_status_and_get_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            status = app.status()
            self.assertTrue(status["ok"])
            self.assertEqual(status["model"], "m1")
            self.assertIn("m1", status["models"])
            self.assertEqual(status["thinking_level"], "off")
            self.assertEqual(status["shell_permission"], "ask")
            self.assertEqual(status["fs_permission"], "ask")
            models = app.get_models()
            self.assertEqual(models["default_model"], "m1")

    def test_ui_prefs_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            # 初始为空
            prefs = app.get_ui_prefs()
            self.assertTrue(prefs["ok"])
            self.assertEqual(prefs["prefs"], {})
            # 写入面板宽度 / 工作区条显隐
            saved = app.set_ui_prefs({"rp_width": 420, "wsbar_hidden": True})
            self.assertTrue(saved["ok"])
            loaded = app.get_ui_prefs()
            self.assertEqual(loaded["prefs"]["rp_width"], 420)
            self.assertTrue(loaded["prefs"]["wsbar_hidden"])
            # 覆盖写入
            app.set_ui_prefs({"rp_width": 360, "wsbar_hidden": False})
            loaded = app.get_ui_prefs()
            self.assertEqual(loaded["prefs"]["rp_width"], 360)
            self.assertFalse(loaded["prefs"]["wsbar_hidden"])
            # 非法入参
            self.assertFalse(app.set_ui_prefs("bad")["ok"])

    def test_export_session_to_workspace_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            app.chat("你好", "t1")
            res = app.export_session("t1")
            self.assertTrue(res["ok"])
            dest = Path(res["path"])
            self.assertEqual(dest.parent, Path(host.workspace) / "exports")
            self.assertEqual(dest.name, "t1.md")
            self.assertIn("## 用户", dest.read_text(encoding="utf-8"))
            # 不存在 / 空会话 → 友好错误
            missing = app.export_session("missing")
            self.assertFalse(missing["ok"])
            self.assertIn("不存在", missing["error"])

    def test_new_session_friendly_default_name_and_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            created = app.new_session()
            sid = created["session"]
            items = {s["id"]: s for s in app.sessions()["sessions"]}
            # 新会话默认名不再是裸 id（s-2026...）
            self.assertNotEqual(items[sid]["name"], sid)
            self.assertTrue(items[sid]["name"].startswith("会话 "))
            # 历史遗留：meta 里名字就是裸 id → 显示层兜底
            meta = dict(host.profile.load_config().get("sessions_meta") or {})
            meta["s-20200101-000000"] = {"name": "s-20200101-000000",
                                         "ws": "", "updated": 0}
            host.profile.update_config(sessions_meta=meta)
            items = {s["id"]: s for s in app.sessions()["sessions"]}
            self.assertEqual(items["s-20200101-000000"]["name"], "未命名会话")

    def test_delete_session_file_meta_and_current_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            app.chat("第一句", "s1")
            app.chat("第二句", "s2")  # s2 更新时间更新
            # 删非当前会话
            app.new_session()          # 当前变为新 id
            current = app.sessions()["current"]
            res = app.delete_session("s1")
            self.assertTrue(res["ok"])
            self.assertEqual(res["deleted"], "s1")
            self.assertEqual(res["next"], "")
            self.assertEqual(host.profile.load_session("s1"), [])
            ids = {s["id"] for s in app.sessions()["sessions"]}
            self.assertNotIn("s1", ids)
            # 删除当前会话 → 自动切到最近的剩余会话
            res = app.delete_session(current)
            self.assertTrue(res["ok"])
            self.assertEqual(res["next"], "s2")
            self.assertEqual(app.sessions()["current"], "s2")
            # 只命名过、还没有消息的会话（仅 meta，无文件）也能删
            fresh = app.new_session()
            app.rename_session(fresh["session"], "空会话")
            res = app.delete_session(fresh["session"])
            self.assertTrue(res["ok"])
            self.assertEqual(app.sessions()["current"], "s2")
            # 不存在 → 报错
            bad = app.delete_session("s1")
            self.assertFalse(bad["ok"])
            self.assertIn("不存在", bad["error"])
            # 非法 id（路径穿越）→ 报错，不删任何文件
            evil = app.delete_session("../escape")
            self.assertFalse(evil["ok"])

    def test_chat_roundtrip_reasoning_and_session_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            from nanoagent.llm import LLMResponse

            host, app = self._app(Path(tmp))
            agent = host.service("agent_factory")()
            agent.llm.script = [LLMResponse(content="答案", reasoning="想一想")]
            result = app.chat("你好", "t1")
            self.assertTrue(result["ok"])
            self.assertEqual(result["reply"], "答案")
            self.assertEqual(result["reasoning"], "想一想")
            sessions = app.sessions()
            self.assertIn("t1", [s["id"] for s in sessions["sessions"]])
            history = app.session_history("t1")
            self.assertEqual(len(history["history"]), 2)
            # 思考过程随 assistant 消息落盘：二次进入会话（回放）能重建思考块
            self.assertEqual(history["history"][1].get("reasoning"), "想一想")

    def test_chat_error_returned_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp), models=[])  # 无模型 → 构建失败
            from harness.cli import build_runtime

            host = build_runtime(profile, str(tmp))
            from harness.desktop import DesktopApp

            app = DesktopApp(host)
            result = app.chat("你好", "t1")
            self.assertFalse(result["ok"])
            self.assertIn("模型", result["error"])

    def test_session_switch_replays_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            app.chat("第一句", "old")
            result = app.chat("第二句", "old")
            self.assertTrue(result["ok"])
            self.assertEqual(len(host.profile.load_session("old")), 4)

    def test_session_rename_and_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            app.chat("第一句", "t1")
            renamed = app.rename_session("t1", "我的会话")
            self.assertTrue(renamed["ok"])
            sessions = {s["id"]: s for s in app.sessions()["sessions"]}
            self.assertEqual(sessions["t1"]["name"], "我的会话")
            self.assertGreater(sessions["t1"]["updated"], 0)
            # 新会话带 meta
            created = app.new_session()
            sessions = {s["id"]: s for s in app.sessions()["sessions"]}
            self.assertIn(created["session"], sessions)

    def test_save_models_validates_and_hot_rebuilds(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            bad = app.save_models([{"name": "m2", "provider": "openai", "model": "y"}])
            self.assertFalse(bad["ok"])
            bad2 = app.save_models([{"name": "m2", "provider": "palm", "model": "y", "api_key": "k"}])
            self.assertFalse(bad2["ok"])
            good = app.save_models([
                {"name": "m1", "provider": "openai", "model": "x", "api_key": "k"},
                {"name": "m2", "provider": "anthropic", "base_url": "https://example.com",
                 "model": "y", "api_key": "k2"},
            ], default_model="m2")
            self.assertTrue(good["ok"], good)
            self.assertEqual(host.profile.load_config()["default_model"], "m2")
            runtime = host.service("models_runtime")
            self.assertIn("m2", runtime["pool"])
            switched = app.switch_model("m1")
            self.assertTrue(switched["ok"])

    def test_thinking_level_applies_to_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            result = app.set_thinking("high")
            self.assertTrue(result["ok"])
            self.assertEqual(host.profile.load_config()["thinking_level"], "high")
            runtime = host.service("models_runtime")
            for llm in runtime["pool"].values():
                self.assertEqual(getattr(llm, "reasoning_effort", None), "high")
            off = app.set_thinking("off")
            self.assertTrue(off["ok"])
            for llm in runtime["pool"].values():
                # off 也显式标记（llm 端会转成 enable_thinking=false 下发），
                # 不再留 None 给服务端默认——否则默认开思考的端点关不掉
                self.assertEqual(getattr(llm, "reasoning_effort", None), "off")
            bad = app.set_thinking("ultra")
            self.assertFalse(bad["ok"])

    def test_usage_and_context_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            app.chat("你好", "t1")
            usage = app.usage()
            self.assertTrue(usage["ok"])
            self.assertIn("prompt_tokens", usage["usage"])
            ctx = app.context_usage()
            self.assertTrue(ctx["ok"], ctx)
            self.assertEqual(ctx["window"], 128000)  # FakeLLM 无 context_window → 默认
            self.assertGreater(ctx["used"], 0)
            names = {item["name"] for item in ctx["breakdown"]}
            self.assertIn("消息", names)
            self.assertIn("系统提示词", names)

    def test_permission_roundtrip_shell_and_fs(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            result = app.set_permission("shell", "allow")
            self.assertTrue(result["ok"])
            self.assertEqual(app.status()["shell_permission"], "allow")
            fs = app.set_permission("fs", "ask")
            self.assertTrue(fs["ok"])
            self.assertEqual(app.status()["fs_permission"], "ask")
            self.assertEqual(host.profile.load_config()["permissions"]["fs"], "ask")
            bad = app.set_permission("shell", "yolo")
            self.assertFalse(bad["ok"])
            bad2 = app.set_permission("disk", "allow")
            self.assertFalse(bad2["ok"])

    def test_fs_write_gate_ask_denies_without_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            app.set_permission("fs", "ask")
            write = next(t for t in host.collect_tools() if t.name == "write_file")
            # DesktopApp 无窗口 → confirm fail-closed
            result = write.invoke({"path": "a.txt", "content": "x"})
            self.assertIn("拒绝", result)
            self.assertFalse((Path(tmp) / "a.txt").exists())

    def test_fs_write_gate_allow_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            app.set_permission("fs", "allow")
            write = next(t for t in host.collect_tools() if t.name == "write_file")
            write.invoke({"path": "a.txt", "content": "x"})
            self.assertTrue((Path(tmp) / "a.txt").is_file())

    def test_confirm_via_in_page_dialog(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            app._window = FakeWindow([True])   # 第一次轮询即返回 True
            self.assertTrue(host.confirm("执行命令?"))
            app._window = FakeWindow([False])
            self.assertFalse(host.confirm("执行命令?"))
            app._window = None
            self.assertFalse(host.confirm("执行命令?"))  # 无窗口 fail-closed

    def test_set_workspace_registry_and_rename(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ws2:
            host, app = self._app(Path(tmp))
            result = app.set_workspace(ws2)
            self.assertTrue(result["ok"])
            self.assertEqual(host.workspace, str(Path(ws2).resolve()))
            self.assertEqual(host.profile.load_config()["workspace"], str(Path(ws2).resolve()))
            workspaces = app.workspaces()["workspaces"]
            self.assertEqual(len(workspaces), 1)
            self.assertEqual(workspaces[0]["name"], Path(ws2).name)
            renamed = app.rename_workspace("我的项目")
            self.assertTrue(renamed["ok"])
            workspaces = app.workspaces()["workspaces"]
            self.assertEqual(workspaces[0]["name"], "我的项目")
            self.assertEqual(app.status()["workspace_name"], "我的项目")
            # 再次切换同一路径 → 复用条目
            again = app.set_workspace(ws2)
            self.assertTrue(again["ok"])
            self.assertEqual(len(app.workspaces()["workspaces"]), 1)
            bad = app.set_workspace(str(Path(tmp) / "no-such-dir"))
            self.assertFalse(bad["ok"])

    def test_workspace_hot_switch_updates_jail(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ws2:
            host, app = self._app(Path(tmp))
            allow_fs(host)   # 本用例测的是路径监狱随工作区热切换，不是权限门
            write = next(t for t in host.collect_tools() if t.name == "write_file")
            write.invoke({"path": "a.txt", "content": "one"})
            self.assertTrue((Path(tmp) / "a.txt").is_file())
            app.set_workspace(ws2)
            write.invoke({"path": "b.txt", "content": "two"})
            self.assertTrue((Path(ws2) / "b.txt").is_file())
            self.assertFalse((Path(tmp) / "b.txt").exists())
            with self.assertRaises(Exception):
                write.invoke({"path": "../escape.txt", "content": "x"})

    def test_choose_workspace_without_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            self.assertFalse(app.choose_workspace()["ok"])

    def test_plugin_install_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            src = Path(tmp) / "my-plugin"
            src.mkdir()
            (src / "plugin.json").write_text('{"name": "my-plugin"}', encoding="utf-8")
            (src / "register.py").write_text(
                "def register(ctx):\n    ctx.provide('ok', 1)", encoding="utf-8")
            result = app.install_plugin(str(src))
            self.assertTrue(result["ok"], result)
            self.assertIn("my-plugin", [p["name"] for p in app.plugins()["plugins"]])
            app.remove_plugin("my-plugin")
            self.assertNotIn("my-plugin", [p["name"] for p in app.plugins()["plugins"]])

    def test_plugin_install_dirname_differs_from_manifest_name(self):
        # H-06 回归：目录名 ≠ manifest name 时也必须真正热激活，且 ok 反映真实状态
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            src = Path(tmp) / "my-plugin-dir"           # 目录名
            src.mkdir()
            (src / "plugin.json").write_text('{"name": "cool-name"}', encoding="utf-8")
            (src / "register.py").write_text(
                "def register(ctx):\n    ctx.provide('cool', 42)", encoding="utf-8")
            result = app.install_plugin(str(src))
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["name"], "cool-name")   # 用 manifest name 上报
            self.assertIn("cool-name", [p["name"] for p in app.plugins()["plugins"]])
            self.assertEqual(host.service("cool"), 42)       # 确认真的热激活
            # 移除时按 manifest name 也要删除真实目录（目录名不同，P2 #14）
            removed = app.remove_plugin("cool-name")
            self.assertTrue(removed["ok"], removed)
            self.assertFalse((host.profile.plugins_dir / "my-plugin-dir").exists())

    def test_plugin_remove_missing_reports_error(self):
        # P2 #14 回归：目标不存在时不再返回静默 ok:true 的半卸载状态
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            result = app.remove_plugin("不存在的插件")
            self.assertFalse(result["ok"], result)

    def test_theme_persist_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            self.assertEqual(app.status()["theme"], "light")  # 默认浅色
            for mode in ("dark", "light", "system"):
                result = app.set_theme(mode)
                self.assertTrue(result["ok"])
                self.assertEqual(app.status()["theme"], mode)
            bad = app.set_theme("blue")
            self.assertFalse(bad["ok"])

    def test_validate_url_blocks_private_and_non_http(self):
        from harness.desktop import _validate_url

        with self.assertRaises(ValueError):
            _validate_url("ftp://example.com/x")
        with self.assertRaises(ValueError):
            _validate_url("http://localhost/x")
        with self.assertRaises(ValueError):
            _validate_url("http://127.0.0.1/x")
        with self.assertRaises(ValueError):
            _validate_url("http://192.168.1.5/x")
        with self.assertRaises(ValueError):
            _validate_url("http://10.0.0.1/x")
        with self.assertRaises(ValueError):
            _validate_url("http://169.254.1.1/x")
        with self.assertRaises(ValueError):
            _validate_url("http://[::1]/x")
        with self.assertRaises(ValueError):
            _validate_url("not-a-url")
        # 公网域名应通过校验
        self.assertEqual(_validate_url("https://raw.githubusercontent.com/x/y"), "raw.githubusercontent.com")

    def test_parse_market_entries_and_categories(self):
        from harness.desktop import _parse_market

        markdown = "\n".join([
            "# Awesome DSH Plugin",
            "## 目录",
            "## 插件",
            "### 🎨 UI 增强",
            "- [a/dsh-skin](https://github.com/a/dsh-skin) — Set of skins for UI",
            "- [b/dsh-bar](https://github.com/b/dsh-bar) - 状态栏插件",  # 半角破折号也兼容
            "### 🧠 记忆",
            "- [c/dsh-memory](https://github.com/c/dsh-memory) — 跨项目记忆",
        ])
        items, categories = _parse_market(markdown)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["name"], "a/dsh-skin")
        self.assertEqual(items[0]["url"], "https://github.com/a/dsh-skin")
        self.assertEqual(items[0]["desc"], "Set of skins for UI")
        self.assertEqual(items[0]["category"], "🎨 UI 增强")
        self.assertEqual(items[2]["desc"], "跨项目记忆")
        self.assertEqual(categories, ["🎨 UI 增强", "🧠 记忆"])

    def test_market_list_uses_cached_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            calls = []
            import harness.desktop as desktop_mod

            original = desktop_mod._fetch_text

            def fake_fetch(url, timeout=20):
                calls.append(url)
                return ("### 🧠 记忆\n"
                        "- [c/dsh-memory](https://github.com/c/dsh-memory) — 跨项目记忆")

            desktop_mod._fetch_text = fake_fetch
            try:
                first = app.market_list()
                self.assertTrue(first["ok"], first)
                self.assertEqual(first["count"], 1)
                second = app.market_list()
                self.assertEqual(len(calls), 1)  # 第二次走缓存
                self.assertEqual(second["items"][0]["name"], "c/dsh-memory")
                forced = app.market_list(force=True)
                self.assertEqual(len(calls), 2)  # 强制刷新
                self.assertTrue(forced["ok"])
            finally:
                desktop_mod._fetch_text = original

    def test_market_list_reports_fetch_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            import harness.desktop as desktop_mod

            def boom(url, timeout=20):
                raise OSError("网络不可达")

            original = desktop_mod._fetch_text
            desktop_mod._fetch_text = boom
            try:
                result = app.market_list()
                self.assertFalse(result["ok"])
                self.assertIn("网络", result["error"])
            finally:
                desktop_mod._fetch_text = original

    def test_market_install_rejects_unsafe_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            self.assertFalse(app.market_install("http://127.0.0.1/repo")["ok"])
            self.assertFalse(app.market_install("ftp://github.com/a/b")["ok"])
            self.assertFalse(app.market_install("")["ok"])

    def test_market_install_clones_and_installs(self):
        import shutil
        import subprocess
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            # 本地 git 仓库模拟远端，把 git clone 替换为复制目录
            remote = Path(tmp) / "remote"
            remote.mkdir()
            (remote / "plugin.json").write_text('{"name": "market-plugin"}', encoding="utf-8")
            (remote / "register.py").write_text("def register(ctx):\n    pass", encoding="utf-8")

            real_run = subprocess.run

            def fake_run(cmd, **kwargs):
                if list(cmd[:3]) == ["git", "clone", "--depth"]:
                    shutil.copytree(remote, cmd[5])  # cmd: git clone --depth 1 <url> <dest>
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                return real_run(cmd, **kwargs)

            with mock.patch("subprocess.run", fake_run):
                result = app.market_install("https://github.com/demo/market-plugin")
            self.assertTrue(result["ok"], result)
            self.assertIn("来源", result.get("note", ""))
            self.assertIn("market-plugin", [p["name"] for p in app.plugins()["plugins"]])

    def test_ws_tree_lists_and_jails(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ws:
            host, app = self._app(Path(tmp))
            app.set_workspace(ws)
            (Path(ws) / "sub").mkdir()
            (Path(ws) / "sub" / "a.py").write_text("x = 1", encoding="utf-8")
            (Path(ws) / ".hidden").mkdir()
            tree = app.ws_tree()
            self.assertTrue(tree["ok"], tree)
            self.assertEqual(tree["rel"], "")
            names = [e["name"] for e in tree["entries"]]
            self.assertEqual(names, ["sub"])  # 点开头条目被隐藏，目录排前
            sub = app.ws_tree("sub")
            self.assertTrue(sub["ok"])
            self.assertEqual(sub["rel"], "sub")
            self.assertEqual([e["name"] for e in sub["entries"]], ["a.py"])
            self.assertFalse(sub["entries"][0]["dir"])
            self.assertGreater(sub["entries"][0]["size"], 0)
            jail = app.ws_tree("../..")
            self.assertFalse(jail["ok"])  # 越出工作区被拒绝
            host.workspace = ""
            self.assertFalse(app.ws_tree()["ok"])

    def test_terminal_reuses_shell_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            app.set_permission("shell", "deny")
            res = app.run_terminal("echo hi")
            self.assertTrue(res["ok"])
            self.assertIn("拒绝", res["output"])  # 权限门生效
            app.set_permission("shell", "allow")
            res2 = app.run_terminal("echo sha-terminal-ok")
            self.assertTrue(res2["ok"])
            self.assertIn("sha-terminal-ok", res2["output"])

    def test_review_requires_git_workspace(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            app.set_workspace(tmp)
            bad = app.review()
            self.assertFalse(bad["ok"])  # 非 git 仓库
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=False)
            (Path(tmp) / "w.txt").write_text("hello", encoding="utf-8")
            good = app.review()
            self.assertTrue(good["ok"], good)
            self.assertIn("w.txt", good["text"])
            self.assertIn("git diff", good["text"])

    def test_skills_install_list_remove(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as skill_src:
            _host, app = self._app(Path(tmp))
            src = Path(skill_src) / "my-skill"
            src.mkdir()
            (src / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: 测试技能\n---\n正文", encoding="utf-8")
            bad = app.install_skill(str(Path(skill_src) / "empty"))  # 不存在
            self.assertFalse(bad["ok"])
            result = app.install_skill(str(src))
            self.assertTrue(result["ok"], result)
            skills = {s["name"]: s for s in app.skills()["skills"]}
            self.assertIn("my-skill", skills)
            self.assertEqual(skills["my-skill"]["source"], "profile")
            # 同名覆盖安装
            (src / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: v2\n---\n正文", encoding="utf-8")
            again = app.install_skill(str(src))
            self.assertTrue(again["ok"])
            skills = {s["name"]: s for s in app.skills()["skills"]}
            self.assertEqual(skills["my-skill"]["desc"], "v2")
            removed = app.remove_skill("my-skill")
            self.assertTrue(removed["ok"])
            self.assertNotIn("my-skill", [s["name"] for s in app.skills()["skills"]])
            outside = app.remove_skill("../../escape")
            self.assertFalse(outside["ok"])  # 不能越出 profile skills 目录

    def test_skills_install_rejects_degenerate_source(self):
        """审计 H-10a：source='.' 时 src.name 为空，旧实现会 rmtree 删光整个 skills 目录。"""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as skill_src:
            _host, app = self._app(Path(tmp))
            src = Path(skill_src) / "my-skill"
            src.mkdir()
            (src / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: 测试技能\n---\n正文", encoding="utf-8")
            # 先装一个正常技能，作为「不能被删光」的对照物
            self.assertTrue(app.install_skill(str(src))["ok"])

            # 场景 A：在含 SKILL.md 的目录里调 install_skill('.') —— Path('.').name == ''
            # （旧行为：dest 退化成 skills 目录本身，rmtree 同名覆盖删光技能库）
            (Path(skill_src) / "SKILL.md").write_text(
                "---\nname: parent\ndescription: 父目录也是技能\n---\n正文", encoding="utf-8")
            cwd = os.getcwd()
            try:
                os.chdir(skill_src)
                bad = app.install_skill(".")
            finally:
                os.chdir(cwd)
            self.assertFalse(bad["ok"], bad)
            self.assertIn("技能文件夹", bad.get("error", ""))
            # 既有技能必须完好
            skills = {s["name"] for s in app.skills()["skills"]}
            self.assertIn("my-skill", skills)

    def test_chat_emits_step_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            from nanoagent.llm import LLMResponse, ToolCall

            host, app = self._app(Path(tmp))
            agent = host.service("agent_factory")()  # 池里的 FakeLLM 与 chat_loop 共用
            agent.llm.script = [
                LLMResponse(content="", tool_calls=[
                    ToolCall(id="1", name="run_command", arguments={"command": "echo hi"})]),
                LLMResponse(content="完成", reasoning="想一想"),
            ]
            events = []
            app._push_event = events.append  # 拦截事件推送
            result = app.chat("跑一下工具", "t-events")
            self.assertTrue(result["ok"], result)
            self.assertTrue(events, "应当产生步骤事件")
            kinds = [e["kind"] for e in events]
            self.assertIn("llm", kinds)
            self.assertIn("tool", kinds)
            tool_evt = next(e for e in events if e["kind"] == "tool")
            self.assertEqual(tool_evt["tool"], "run_command")
            llm_evts = [e for e in events if e["kind"] == "llm"]
            self.assertEqual(llm_evts[-1]["reasoning"], "想一想")


class DesktopStreamTests(unittest.TestCase):
    """桌面端流式输出（chat_stream/can_stream）：事件推送、会话落盘、用量补记。"""

    def _app(self, tmp: Path):
        from harness.desktop import DesktopApp

        with patch_model_build():
            host = build_host(tmp)
        return host, DesktopApp(host)

    def _stream_app(self, tmp: Path):
        """用 StreamingFakeLLM 构建运行时（激活期就注入，模型池里才是流式模型）。"""
        import nanoagent
        from test_harness import StreamingFakeLLM

        from harness.desktop import DesktopApp

        profile = make_profile(tmp)
        from harness.cli import build_runtime

        orig = nanoagent.LLM
        nanoagent.LLM = lambda **kwargs: StreamingFakeLLM()
        try:
            host = build_runtime(profile, str(tmp))
        finally:
            nanoagent.LLM = orig
        app = DesktopApp(host)
        window = FakeWindow([])
        app._window = window
        return host, app, window

    @staticmethod
    def _events(window, kind: str) -> list[dict]:
        import json

        found = []
        for js in window.calls:
            if "onStreamEvent(" in js:
                payload = js.split("onStreamEvent(", 1)[1].rsplit(")", 1)[0]
                try:
                    evt = json.loads(payload)
                except ValueError:
                    continue
                if evt.get("kind") == kind:
                    found.append(evt)
        return found

    def test_can_stream_false_without_stream_llm(self):
        with tempfile.TemporaryDirectory() as tmp:

            _host, app = self._app(Path(tmp))  # FakeLLM 无 chat_stream
            self.assertFalse(app.can_stream()["stream"])

    def test_chat_stream_pushes_delta_and_done(self):
        import time

        from test_harness import StreamingFakeLLM

        with tempfile.TemporaryDirectory() as tmp:
            host, app, window = self._stream_app(Path(tmp))
            self.assertTrue(app.can_stream()["stream"])
            res = app.chat_stream("你好", "s1")
            self.assertTrue(res["ok"])
            self.assertTrue(res["stream"])
            self.assertEqual(res["session"], "s1")
            self.assertIn("s1", app._busy_sessions)  # 立即进入该会话的流式状态（防双发）

            deadline = time.time() + 5
            done: list[dict] = []
            while time.time() < deadline:
                done = self._events(window, "done")
                if done:
                    break
                time.sleep(0.05)
            self.assertTrue(done, "5 秒内应收到 done 事件")
            self.assertTrue(done[0]["ok"])
            self.assertEqual(done[0]["reply"], "".join(StreamingFakeLLM.chunks))
            self.assertEqual(done[0]["session"], "s1")
            self.assertEqual(done[0]["model"], "m1")
            deltas = self._events(window, "delta")
            self.assertTrue(deltas, "应有 delta 增量事件")
            # done 事件先于 worker 收尾推送，等会话真正落盘再收尾
            # （直接轮询落盘结果，避免与 worker 的收尾写入产生文件竞态）
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    if len(host.profile.load_session("s1")) >= 2:
                        break
                except Exception:  # noqa: BLE001 —— 落盘中途读失败继续等
                    pass
                time.sleep(0.05)
            self.assertEqual(len(host.profile.load_session("s1")), 2)
            # 用量已补记（此前流式轮次在 usage.jsonl 是空白）——落盘晚于会话，轮询等待
            deadline = time.time() + 5
            while time.time() < deadline and not (host.profile.root / "usage.jsonl").is_file():
                time.sleep(0.05)
            self.assertTrue((host.profile.root / "usage.jsonl").is_file())
            # 双开防护：流已结束可再次发起
            self.assertTrue(app.chat_stream("再来", "s1")["ok"])
            # 等第二轮流收尾（含落盘），否则临时目录清理会撞上在写的 tmp 文件
            deadline = time.time() + 5
            while "s1" in app._busy_sessions and time.time() < deadline:
                time.sleep(0.05)

    def test_chat_stream_error_pushed_as_done(self):
        import time

        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp), models=[])  # 无模型 → ask_stream 抛错
            from harness.cli import build_runtime

            host = build_runtime(profile, str(tmp))
            from harness.desktop import DesktopApp

            app = DesktopApp(host)
            window = FakeWindow([])
            app._window = window
            res = app.chat_stream("你好", "s1")
            self.assertTrue(res["ok"])  # 派发成功，错误在事件里
            deadline = time.time() + 5
            done: list[dict] = []
            while time.time() < deadline:
                done = self._events(window, "done")
                if done:
                    break
                time.sleep(0.05)
            self.assertTrue(done and not done[0]["ok"])
            self.assertIn("模型", done[0].get("error", ""))

    def test_ask_stream_should_stop_truncates(self):
        """should_stop 透传 nanoagent：命中后只放行已收的 delta，无 done，不落盘。"""
        from test_harness import StreamingFakeLLM

        with tempfile.TemporaryDirectory() as tmp:
            host, _app, _window = self._stream_app(Path(tmp))
            ask_stream = host.service("ask_stream")
            calls = {"n": 0}

            def stop():
                calls["n"] += 1
                return calls["n"] > 2   # 循环开始 + 首个 delta 前放行，之后停

            events = list(ask_stream("你好", "s-stop", should_stop=stop))
            self.assertEqual([e["type"] for e in events], ["delta"])
            self.assertEqual(events[0]["text"], StreamingFakeLLM.chunks[0])
            # 中断的回合不落盘（与 ask 中断即不落盘一致）
            self.assertEqual(host.profile.load_session("s-stop"), [])

    def test_stop_stream_sets_cancel_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            res = app.stop_stream("sx")
            self.assertTrue(res["ok"])
            self.assertEqual(res["session"], "sx")
            self.assertIn("sx", app._cancel_sessions)


class AutoTitleTests(unittest.TestCase):
    """会话自动起名：首轮回复后给默认命名的会话起标题（config.auto_title=false 关）。"""

    def _app(self, tmp: Path):
        from harness.desktop import DesktopApp

        with patch_model_build():
            host = build_host(tmp)
        return host, DesktopApp(host)

    def _meta_name(self, host, sid: str) -> str:
        return str((host.profile.load_config().get("sessions_meta") or {})
                   .get(sid, {}).get("name") or "")

    def test_first_reply_titles_default_named_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            from nanoagent.llm import LLMResponse

            host, app = self._app(Path(tmp))
            agent = host.service("agent_factory")()
            # 第 1 个响应给对话，第 2 个给起名调用
            agent.llm.script = [LLMResponse(content="答案"),
                                LLMResponse(content=" 爬虫开发实战 ")]
            app.chat("帮我写爬虫", "t9")
            self.assertEqual(self._meta_name(host, "t9"), "爬虫开发实战")

    def test_renamed_session_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            app.rename_session("t10", "我的专属会话")
            app.chat("你好", "t10")   # 起名调用不会被触发（FakeLLM 无需脚本）
            self.assertEqual(self._meta_name(host, "t10"), "我的专属会话")

    def test_auto_title_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            from nanoagent.llm import LLMResponse

            host, app = self._app(Path(tmp))
            host.profile.update_config(auto_title=False)
            agent = host.service("agent_factory")()
            agent.llm.script = [LLMResponse(content="答案"),
                                LLMResponse(content="不该出现的标题")]
            app.chat("帮我写爬虫", "t11")
            name = self._meta_name(host, "t11")
            self.assertTrue(name.startswith("会话 "))
            self.assertNotEqual(name, "不该出现的标题")


class McpSettingsTests(unittest.TestCase):
    """MCP 服务器可视化管理：保存校验 / 掩码回显与原值还原 / 热重连副作用。"""

    def _app(self, tmp: Path):
        from harness.desktop import DesktopApp

        with patch_model_build():
            host = build_host(tmp)
        return host, DesktopApp(host)

    def test_save_list_and_mask_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            res = app.save_mcp([
                {"name": "fs", "type": "stdio", "command": "no-such-cmd-xyz",
                 "args": "-y demo", "env": {"API_KEY": "sk-secret-key-1"}},
                {"name": "remote", "type": "http", "url": "https://example.com/mcp",
                 "headers": {"Authorization": "Bearer tok-12345678"}},
            ])
            self.assertTrue(res["ok"])
            self.assertEqual(res["count"], 2)
            cfg = host.profile.load_config()["mcpServers"]
            self.assertEqual(cfg["fs"]["command"], "no-such-cmd-xyz")
            self.assertEqual(cfg["fs"]["args"], ["-y", "demo"])
            self.assertEqual(cfg["fs"]["env"]["API_KEY"], "sk-secret-key-1")
            self.assertEqual(cfg["remote"]["url"], "https://example.com/mcp")
            # 列表回显掩码；保存时回传掩码 → 还原原值（保存即全量替换，remote 消失）
            listed = {s["name"]: s for s in app.mcp_servers()["servers"]}
            self.assertIn("***", listed["fs"]["env"]["API_KEY"])
            self.assertIn("***", listed["remote"]["headers"]["Authorization"])
            res2 = app.save_mcp([
                {"name": "fs", "type": "stdio", "command": "no-such-cmd-xyz",
                 "env": {"API_KEY": listed["fs"]["env"]["API_KEY"]}},
            ])
            self.assertTrue(res2["ok"])
            self.assertEqual(res2["count"], 1)
            cfg2 = host.profile.load_config()["mcpServers"]
            self.assertEqual(cfg2["fs"]["env"]["API_KEY"], "sk-secret-key-1")
            self.assertNotIn("remote", cfg2)
            # 保存后 agent 被丢弃（reset_agent 服务生效，下轮 ask 重建工具集）
            self.assertIsNotNone(host.service("reset_agent"))

    def test_save_mcp_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            self.assertFalse(app.save_mcp(
                [{"name": "a", "type": "stdio", "command": ""}])["ok"])
            bad_url = app.save_mcp([{"name": "b", "type": "http", "url": "ftp://x"}])
            self.assertFalse(bad_url["ok"])
            self.assertIn("http", bad_url["error"])
            dup = app.save_mcp([
                {"name": "dup", "type": "stdio", "command": "x"},
                {"name": "dup", "type": "stdio", "command": "y"},
            ])
            self.assertFalse(dup["ok"])
            self.assertIn("重名", dup["error"])
            # 校验失败不写配置
            self.assertIsNone(host.profile.load_config().get("mcpServers"))


class Batch11FeatureTests(unittest.TestCase):
    """第六批十一项功能：搜索/置顶/重新生成/编辑重发/导出全部/用量图表/快捷指令/知识库来源。"""

    def _app(self, tmp: Path):
        from harness.desktop import DesktopApp

        with patch_model_build():
            host = build_host(tmp)
        return host, DesktopApp(host)

    def test_pin_session_sorts_first_in_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            app.chat("一", "s1")
            app.chat("二", "s2")
            self.assertTrue(app.set_session_pin("s1", True)["ok"])
            items = app.sessions()["sessions"]
            self.assertEqual(items[0]["id"], "s1")
            self.assertTrue(items[0]["pinned"])
            self.assertFalse(items[1]["pinned"])
            # 取消置顶 → 恢复按更新时间排序（s2 更新）
            app.set_session_pin("s1", False)
            items = app.sessions()["sessions"]
            self.assertEqual(items[0]["id"], "s2")
            # 缺 id → 报错
            self.assertFalse(app.set_session_pin("", True)["ok"])

    def test_search_sessions_current_and_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            app.chat("今天天气如何", "s1")
            app.chat("写一首诗", "s2")  # 当前会话变为 s2
            res = app.search_sessions("天气", "all")
            self.assertTrue(res["ok"])
            self.assertTrue(any(h["session"] == "s1" for h in res["hits"]))
            # 仅当前会话（s2）→ 搜不到 s1 的内容
            res_cur = app.search_sessions("天气", "current")
            self.assertFalse(any(h["session"] == "s1" for h in res_cur["hits"]))
            # 命中项带可定位的过滤后序号与摘要
            hit = next(h for h in res["hits"] if h["session"] == "s1")
            self.assertEqual(hit["role"], "user")
            self.assertIn("天气", hit["snippet"])
            # 空关键词 / 未知范围
            self.assertFalse(app.search_sessions("  ", "all")["ok"])
            self.assertFalse(app.search_sessions("x", "bad")["ok"])

    def test_regenerate_last_replaces_final_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            from nanoagent.llm import LLMResponse

            _host, app = self._app(Path(tmp))
            _host.profile.update_config(auto_title=False)  # 隔离起名功能：脚本按位消费
            agent = _host.service("agent_factory")()
            agent.llm.script = [LLMResponse(content="第一版"), LLMResponse(content="第二版")]
            app.chat("问题", "t1")
            res = app.regenerate_last()
            self.assertTrue(res["ok"], res.get("error"))
            self.assertEqual(res["reply"], "第二版")
            history = app.session_history("t1")["history"]
            self.assertEqual([m["content"] for m in history], ["问题", "第二版"])
            # 没有用户消息（新会话）→ 可读错误
            app.new_session()
            self.assertFalse(app.regenerate_last()["ok"])

    def test_edit_message_resend_truncates_and_reruns(self):
        with tempfile.TemporaryDirectory() as tmp:
            from nanoagent.llm import LLMResponse

            _host, app = self._app(Path(tmp))
            _host.profile.update_config(auto_title=False)  # 隔离起名功能：脚本按位消费
            agent = _host.service("agent_factory")()
            agent.llm.script = [LLMResponse(content="回答1"), LLMResponse(content="回答2")]
            app.chat("原始问题", "t2")
            res = app.edit_message_resend(0, "改后问题")
            self.assertTrue(res["ok"], res.get("error"))
            self.assertEqual(res["reply"], "回答2")
            history = app.session_history("t2")["history"]
            self.assertEqual([m["content"] for m in history], ["改后问题", "回答2"])
            # 只能编辑用户消息
            self.assertFalse(app.edit_message_resend(1, "x")["ok"])
            # 序号越界 / 空文本
            self.assertFalse(app.edit_message_resend(9, "x")["ok"])
            self.assertFalse(app.edit_message_resend(0, "  ")["ok"])

    def test_export_all_sessions_zip_and_cli_flag(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            app.chat("甲", "s1")
            app.chat("乙", "s2")
            res = app.export_all_sessions()
            self.assertTrue(res["ok"], res.get("error"))
            import zipfile

            with zipfile.ZipFile(res["path"]) as zf:
                names = zf.namelist()
            self.assertIn("index.md", names)
            self.assertIn("s1.md", names)
            self.assertIn("s2.md", names)
            # CLI：sha export --all
            from harness.cli import _export_sessions

            ns = SimpleNamespace(session=None, out=str(Path(host.workspace) / "exports"),
                                 all_zip=True, usage=False, workspace=str(host.workspace))
            self.assertEqual(_export_sessions(host.profile, ns), 0)
            # 一个会话都没有 → ValueError 报「没有可导出」
            from harness.exporter import export_all_sessions_zip

            with tempfile.TemporaryDirectory() as empty:
                empty_profile = make_profile(Path(empty), models=[])
                with self.assertRaises(ValueError):
                    export_all_sessions_zip(empty_profile, Path(empty) / "x.zip")

    def test_usage_daily_aggregates_by_local_day(self):
        import json
        import time

        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            now = time.time()
            rows = [
                {"ts": now, "session": "s1", "model": "m",
                 "prompt_tokens": 100, "completion_tokens": 50,
                 "cached_tokens": 40},
                {"ts": now - 86400, "session": "s1", "model": "m",
                 "prompt_tokens": 10, "completion_tokens": 5},
                {"ts": now - 40 * 86400, "session": "s1", "model": "m",
                 "prompt_tokens": 999, "completion_tokens": 999},
                {"这行不是合法 json": True},
            ]
            (host.profile.root / "usage.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8")
            res = app.usage_daily(30)
            self.assertTrue(res["ok"])
            days = res["days"]
            self.assertEqual(len(days), 30)  # 空档天补齐，横轴连续
            today = days[-1]
            self.assertEqual(today["prompt"], 100)
            self.assertEqual(today["total"], 150)
            self.assertEqual(today["calls"], 1)
            self.assertEqual(today["cached"], 40)  # 缓存命中 tokens 单独聚合
            yesterday = days[-2]
            self.assertEqual(yesterday["total"], 15)
            # 40 天前的记录被排除
            self.assertEqual(sum(d["total"] for d in days), 165)

    def test_snippets_roundtrip_via_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            self.assertTrue(app.snippets_save("审查", "请审查改动")["ok"])
            items = app.snippets_list()["snippets"]
            self.assertEqual([i["name"] for i in items], ["审查"])
            # 同名覆盖
            app.snippets_save("审查", "v2")
            items = app.snippets_list()["snippets"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["text"], "v2")
            # 删除（幂等）
            self.assertTrue(app.snippets_delete("审查")["ok"])
            self.assertEqual(app.snippets_list()["snippets"], [])
            self.assertTrue(app.snippets_delete("不存在")["ok"])
            # 校验
            self.assertFalse(app.snippets_save("", "x")["ok"])
            self.assertFalse(app.snippets_save("名", " ")["ok"])

    def test_knowledge_sources_and_remove_source(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            kb_dir = host.profile.root / "knowledge"
            kb_dir.mkdir(parents=True, exist_ok=True)
            (kb_dir / "index.npz").write_text(json.dumps({
                "texts": ["a1", "a2", "b1"],
                "vectors": [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]],
                "metadata": [{"source": "a.md"}, {"source": "a.md"}, {"source": "b.md"}],
            }, ensure_ascii=False), encoding="utf-8")
            res = app.knowledge_sources()
            self.assertTrue(res["ok"], res.get("error"))
            self.assertEqual({s["source"]: s["chunks"] for s in res["sources"]},
                             {"a.md": 2, "b.md": 1})
            # 删除 a.md 的全部片段；其余来源保留（需要构建 KB：池里有 FakeLLM）
            rm = app.knowledge_remove_source("a.md")
            self.assertTrue(rm["ok"], rm.get("summary"))
            self.assertIn("2", rm["summary"])
            res = app.knowledge_sources()
            self.assertEqual([s["source"] for s in res["sources"]], ["b.md"])
            # 来源不存在 → ok=False
            self.assertFalse(app.knowledge_remove_source("不存在.md")["ok"])

    def test_image_preview_returns_data_url_and_jails(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            ws = Path(host.workspace)
            ws.mkdir(parents=True, exist_ok=True)
            # 1x1 PNG（最小心智合理的合法图片字节）
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                "1f15c4890000000d49444154789c6260000000060005"
                "27de41ba0000000049454e44ae426082")
            (ws / "pic.png").write_bytes(png)
            res = app.image_preview(str(ws / "pic.png"))
            self.assertTrue(res["ok"], res.get("error"))
            self.assertTrue(res["data"].startswith("data:image/png;base64,"))
            # 越出工作区 → 拒绝（放在另一个临时目录，确保不在工作区内）
            outside_dir = Path(tempfile.mkdtemp(prefix="sha-outside-"))
            outside = outside_dir / "evil.png"
            outside.write_bytes(png)
            bad = app.image_preview(str(outside))
            self.assertFalse(bad["ok"])
            # 不存在 → 拒绝
            self.assertFalse(app.image_preview(str(ws / "no.png"))["ok"])

class LongrunResetTests(unittest.TestCase):
    """F1/F6 桌面端：TODO.md 任务面板数据源 + 一键无状态重置。"""

    def _app(self, tmp: Path):
        from harness.desktop import DesktopApp

        with patch_model_build():
            host = build_host(tmp)
        return host, DesktopApp(host)

    def test_chat_rebinds_session_workspace(self):
        """跨工作区会话联动：继续旧会话时自动切回它首次使用的工作区。

        修复「问当前工作区文件却答成另一个工作区」——会话历史属于某个工程，
        agent 的工具与上下文必须指回那个工程。
        """
        import os

        from nanoagent.llm import LLMResponse

        from harness.desktop import DesktopApp

        with tempfile.TemporaryDirectory() as tmp:
            with patch_model_build():
                host = build_host(Path(tmp))
            app = DesktopApp(host)
            ws_a = Path(tmp) / "projA"
            ws_b = Path(tmp) / "projB"
            ws_a.mkdir()
            ws_b.mkdir()
            # 在 projA 里开一个会话（打上 projA 的 ws 戳）
            app.set_workspace(str(ws_a))
            agent = host.service("agent_factory")()
            agent.llm.script = [LLMResponse(content="收到")]
            app.chat("在 projA 干活", "s-a")
            meta = host.profile.load_config().get("sessions_meta") or {}
            ws_id_a = meta["s-a"]["ws"]
            self.assertTrue(ws_id_a)
            # 切到 projB 另起炉灶
            app.set_workspace(str(ws_b))
            self.assertEqual(app.status()["workspace"], str(ws_b))
            # 继续旧会话 s-a → 自动切回 projA，工具/上下文指向 projA
            agent.llm.script = [LLMResponse(content="好的")]
            res = app.chat("继续", "s-a")
            self.assertTrue(res["ok"], res.get("error"))
            self.assertEqual(app.status()["workspace"], str(ws_a))
            self.assertEqual(os.path.realpath(host.workspace),
                             os.path.realpath(str(ws_a)))

    def test_todo_content_reads_workspace_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            ws = Path(host.workspace)
            ws.mkdir(parents=True, exist_ok=True)
            res = app.todo_content()
            self.assertTrue(res["ok"])
            self.assertFalse(res["exists"])
            (ws / "TODO.md").write_text(
                "# TODO\n\n- [ ] 任务一\n- [x] 任务二\n", encoding="utf-8")
            res = app.todo_content()
            self.assertTrue(res["ok"])
            self.assertTrue(res["exists"])
            self.assertIn("任务一", res["content"])

    def test_stateless_reset_writes_back_and_switches(self):
        from nanoagent.llm import LLMResponse

        with tempfile.TemporaryDirectory() as tmp:
            _host, app = self._app(Path(tmp))
            _host.profile.update_config(auto_title=False)  # 隔离起名功能：脚本按位消费
            agent = _host.service("agent_factory")()
            agent.llm.script = [LLMResponse(content="收到"),
                                LLMResponse(content="已写入 TODO.md")]
            app.chat("做任务一", "s1")
            r = app.stateless_reset()
            self.assertTrue(r["ok"], r.get("error"))
            self.assertEqual(r["note"], "已写入 TODO.md")
            self.assertNotEqual(r["session"], "s1")
            self.assertEqual(app.sessions()["current"], r["session"])
            # 旧会话保留可回看：含重置前的对话与「写回 TODO.md」这一轮
            old = [m["content"] for m in app.session_history("s1")["history"]]
            self.assertEqual(old[0], "做任务一")
            self.assertEqual(old[-1], "已写入 TODO.md")
            self.assertEqual(len(old), 4)

class GitRemoteTests(unittest.TestCase):
    """远程仓库地址配置（用户手动提交/推送的入口）：get/set 与无 origin 的推送拒绝。"""

    def _app(self, tmp: Path):
        import subprocess

        from harness.desktop import DesktopApp

        with patch_model_build():
            host = build_host(tmp)
        ws = Path(host.workspace)
        ws.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=ws, check=False)
        return DesktopApp(host), ws

    def test_remote_set_and_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _ws = self._app(Path(tmp))
            res = app.git_remote_get()
            self.assertTrue(res["ok"])
            self.assertFalse(res["configured"])
            bad = app.git_remote_set("ftp://example.com/x.git")
            self.assertFalse(bad["ok"])
            res = app.git_remote_set("https://github.com/gwqwy/demo.git")
            self.assertTrue(res["ok"], res.get("error"))
            self.assertEqual(res["action"], "已添加")
            # 再次设置 → 更新而非重复添加
            res = app.git_remote_set("git@github.com:gwqwy/demo.git")
            self.assertTrue(res["ok"])
            self.assertEqual(res["action"], "已更新")
            res = app.git_remote_get()
            self.assertTrue(res["configured"])
            self.assertEqual(res["url"], "git@github.com:gwqwy/demo.git")

    def test_push_without_remote_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _ws = self._app(Path(tmp))
            res = app.git_push()
            self.assertFalse(res["ok"])
            self.assertIn("origin", res["output"])

    def test_plan_mode_toggle_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _ws = self._app(Path(tmp))
            host = app.host
            res = app.set_plan_mode(True)
            self.assertTrue(res["ok"], res.get("error"))
            self.assertIn("已开启", res["message"])
            self.assertTrue(app.status()["plan_mode"])
            agent = host.service("agent_factory")()
            self.assertIn("计划模式（当前开启）", agent.instructions)
            res = app.set_plan_mode(False)
            self.assertTrue(res["ok"])
            self.assertFalse(app.status()["plan_mode"])


class ParallelStreamTests(unittest.TestCase):
    """多会话并行：不同会话可同时回答，同一会话仍拒绝双开。"""

    def test_parallel_streams_per_session(self):
        import threading
        import time as _time
        import types

        from harness.desktop import DesktopApp

        with tempfile.TemporaryDirectory() as tmp:
            with patch_model_build():
                host = build_host(Path(tmp))
            app = DesktopApp(host)
            release: dict[str, threading.Event] = {}
            done_marker: dict[str, bool] = {}

            def fake_ask_stream(message, session_id=None, images=None, should_stop=None):
                ev = release.setdefault(session_id, threading.Event())
                yield {"type": "delta", "text": "hi"}
                ev.wait(2)  # 保持流不结束，便于断言并行/互斥状态
                yield {"type": "done", "result": types.SimpleNamespace(
                    content="回复", reasoning="", tool_calls=[],
                    usage={"prompt_tokens": 1, "completion_tokens": 1})}
                done_marker[session_id] = True

            orig = host.service
            host.service = (lambda name, *a, **k:
                            fake_ask_stream if name == "ask_stream"
                            else orig(name, *a, **k))
            app.set_workspace(str(Path(host.workspace)))
            r1 = app.chat_stream("m1", "s1")
            r2 = app.chat_stream("m2", "s2")
            self.assertTrue(r1["ok"], r1.get("error"))
            self.assertTrue(r2["ok"], r2.get("error"))
            self.assertIn("s1", app._busy_sessions)
            self.assertIn("s2", app._busy_sessions)
            # 同一会话第二个流被拒绝；另一会话不受影响
            r3 = app.chat_stream("m3", "s1")
            self.assertFalse(r3["ok"])
            self.assertIn("进行中", r3["error"])
            r4 = app.chat_stream("m4", "s3")
            self.assertTrue(r4["ok"], r4.get("error"))
            release["s1"].set()
            release["s2"].set()
            release["s3"].set()
            for _ in range(100):
                if not app._busy_sessions:
                    break
                _time.sleep(0.05)
            self.assertEqual(app._busy_sessions, set())
            self.assertTrue(done_marker.get("s1") and done_marker.get("s2"))


class SessionOrderTests(unittest.TestCase):
    """侧栏拖拽排序：set_session_order 持久化 order，sessions() 按序返回。"""

    def test_set_order_persists_and_sorts(self):
        import time as _time

        from harness.desktop import DesktopApp

        with tempfile.TemporaryDirectory() as tmp:
            with patch_model_build():
                host = build_host(Path(tmp))
            app = DesktopApp(host)
            ws_id = "w1"
            now = _time.time()
            host.profile.update_config(
                current_workspace=ws_id,
                workspaces=[{"id": ws_id, "name": "ws", "path": host.workspace}],
                sessions_meta={
                    "a": {"ws": ws_id, "updated": now, "name": "a"},
                    "b": {"ws": ws_id, "updated": now + 5, "name": "b"},
                    "c": {"ws": ws_id, "updated": now + 9, "name": "c"},
                })
            # 默认按最近更新：c, b, a
            ids = [s["id"] for s in app.sessions()["sessions"]]
            self.assertEqual(ids, ["c", "b", "a"])
            # 拖拽成 a, b, c → order 落盘
            res = app.set_session_order(["a", "b", "c"])
            self.assertTrue(res["ok"])
            ids = [s["id"] for s in app.sessions()["sessions"]]
            self.assertEqual(ids, ["a", "b", "c"])
            meta = host.profile.load_config()["sessions_meta"]
            self.assertEqual(meta["a"]["order"], 0)
            self.assertEqual(meta["c"]["order"], 2)


class FilePreviewTests(unittest.TestCase):
    """文件预览：类型判定、图片转 data URL、路径越界拦截。"""

    def test_preview_kinds_and_jail(self):
        import base64

        from harness.desktop import DesktopApp

        with tempfile.TemporaryDirectory() as tmp:
            with patch_model_build():
                host = build_host(Path(tmp))
            app = DesktopApp(host)
            ws = Path(host.workspace)
            (ws / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
            (ws / "b.md").write_text("# 标题\n\n正文\n", encoding="utf-8")
            (ws / "c.csv").write_text("a,b\n1,2\n", encoding="utf-8")
            (ws / "d.bin").write_bytes(b"\x00\x01\x02\x03")
            png = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF/6n2jAAAAAElFTkSuQmCC")
            (ws / "e.png").write_bytes(png)
            (ws / "f.txt").write_text("中文内容\n第二行\n", encoding="gbk")

            r = app.file_preview("a.py")
            self.assertTrue(r["ok"])
            self.assertEqual(r["kind"], "code")
            self.assertIn("def f():", r["text"])
            self.assertGreaterEqual(r["lines"], 2)  # 末尾换行使计数比可见行多 1
            self.assertEqual(app.file_preview("b.md")["kind"], "markdown")
            self.assertEqual(app.file_preview("c.csv")["kind"], "table")
            self.assertEqual(app.file_preview("d.bin")["kind"], "binary")
            # GBK 文本也要能读出中文（编码回退）
            self.assertIn("中文内容", app.file_preview("f.txt")["text"])

            ri = app.file_preview("e.png")
            self.assertEqual(ri["kind"], "image")
            self.assertTrue(ri["data_url"].startswith("data:image/png;base64,"))

            # 路径越界 / 不存在都必须失败
            self.assertFalse(app.file_preview("../outside.py")["ok"])
            self.assertFalse(app.file_preview("nope.txt")["ok"])


class FilePreviewExtTests(unittest.TestCase):
    """文件预览扩展：OOXML 解析 / 压缩包清单 / 二进制摘要 / 媒体 data URL / HTML。"""

    def _app(self, tmp: str):
        from harness.desktop import DesktopApp

        with patch_model_build():
            host = build_host(Path(tmp))
        app = DesktopApp(host)
        ws = Path(tmp) / "ws"
        ws.mkdir(exist_ok=True)
        app.set_workspace(str(ws))
        return app, ws

    def test_preview_ooxml_zip_and_binary(self):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            app, ws = self._app(tmp)
            # docx：提取段落文本
            with zipfile.ZipFile(ws / "a.docx", "w") as zf:
                zf.writestr("word/document.xml",
                            "<w:p><w:r><w:t>你好</w:t></w:r></w:p>"
                            "<w:p><w:r><w:t>世界</w:t></w:r></w:p>")
            r = app.file_preview("a.docx")
            self.assertEqual(r["kind"], "office")
            self.assertIn("你好", r["text"])
            self.assertIn("世界", r["text"])
            # xlsx：共享字符串 + 首表 → TSV
            with zipfile.ZipFile(ws / "b.xlsx", "w") as zf:
                zf.writestr("xl/sharedStrings.xml",
                            "<sst><si><t>甲</t></si><si><t>乙</t></si></sst>")
                zf.writestr("xl/worksheets/sheet1.xml",
                            '<worksheet><sheetData><row>'
                            '<c t="s"><v>0</v></c><c t="s"><v>1</v></c>'
                            "</row></sheetData></worksheet>")
            r = app.file_preview("b.xlsx")
            self.assertEqual(r["kind"], "table")
            self.assertEqual(r["text"], "甲\t乙")
            # pptx：逐页文本
            with zipfile.ZipFile(ws / "c.pptx", "w") as zf:
                zf.writestr("ppt/slides/slide1.xml",
                            "<p:sld><a:t>标题页</a:t></p:sld>")
            r = app.file_preview("c.pptx")
            self.assertEqual(r["kind"], "office")
            self.assertIn("标题页", r["text"])
            # zip：条目清单
            with zipfile.ZipFile(ws / "d.zip", "w") as zf:
                zf.writestr("inner/readme.txt", "hi")
            r = app.file_preview("d.zip")
            self.assertEqual(r["kind"], "archive")
            self.assertIn("inner/readme.txt", r["text"])
            # 二进制：十六进制摘要
            (ws / "e.bin").write_bytes(b"\x00\x01\x02hello")
            r = app.file_preview("e.bin")
            self.assertEqual(r["kind"], "binary")
            self.assertIn("00000000", r["text"])
            # HTML：文本 + html kind
            (ws / "f.html").write_text("<html><body>ok</body></html>",
                                       encoding="utf-8")
            r = app.file_preview("f.html")
            self.assertEqual(r["kind"], "html")
            self.assertIn("<html>", r["text"])

    def test_preview_media_data_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, ws = self._app(tmp)
            (ws / "a.mp3").write_bytes(b"ID3\x04\x00\x00\x00")
            r = app.file_preview("a.mp3")
            self.assertEqual(r["kind"], "audio")
            self.assertTrue(r["data_url"].startswith("data:audio/mpeg;base64,"))
            (ws / "v.webm").write_bytes(b"\x1aE\xdf\xa3")
            r = app.file_preview("v.webm")
            self.assertEqual(r["kind"], "video")
            self.assertTrue(r["data_url"].startswith("data:video/webm;base64,"))


class SubagentLogTests(unittest.TestCase):
    """子 agent 调用记录：读取 profile/subagents.jsonl（最新在前，坏行跳过）。"""

    def test_subagents_reads_jsonl(self):
        import json

        from harness.desktop import DesktopApp

        with tempfile.TemporaryDirectory() as tmp:
            with patch_model_build():
                host = build_host(Path(tmp))
            app = DesktopApp(host)
            path = host.profile.root / "subagents.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"ts": 1, "task": "旧任务", "ok": True}, ensure_ascii=False) + "\n" +
                "{坏行\n" +
                json.dumps({"ts": 2, "task": "新任务", "ok": False}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            r = app.subagents(10)
            self.assertTrue(r["ok"])
            self.assertEqual(len(r["items"]), 2)          # 坏行被跳过
            self.assertEqual(r["items"][0]["task"], "新任务")  # 最新在前
            # 无文件时返回空列表而不是报错
            path.unlink()
            self.assertEqual(app.subagents()["items"], [])

    def test_remove_subagents_by_ids_and_clear(self):
        """按 id 移除单条 / 清空全部；坏行与无 id 旧记录在按 id 删除时保留。"""
        import json

        from harness.desktop import DesktopApp

        with tempfile.TemporaryDirectory() as tmp:
            with patch_model_build():
                host = build_host(Path(tmp))
            app = DesktopApp(host)
            path = host.profile.root / "subagents.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"id": "aaa", "ts": 1, "task": "A", "ok": True}, ensure_ascii=False) + "\n" +
                "{坏行\n" +
                json.dumps({"id": "bbb", "ts": 2, "task": "B", "ok": True}, ensure_ascii=False) + "\n" +
                json.dumps({"ts": 3, "task": "旧记录无id", "ok": True}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            r = app.remove_subagents(["bbb"])
            self.assertTrue(r["ok"])
            self.assertEqual(r["removed"], 1)
            kept_lines = [line for line in
                          path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(json.loads(kept_lines[0])["task"], "A")
            self.assertEqual(kept_lines[1], "{坏行")           # 坏行原样保留
            self.assertEqual(json.loads(kept_lines[2])["task"], "旧记录无id")
            self.assertFalse((path.parent / "subagents.jsonl.tmp").exists())
            # 未命中任何 id → 文件原样
            r = app.remove_subagents(["nope"])
            self.assertTrue(r["ok"])
            self.assertEqual(r["removed"], 0)
            self.assertEqual(len(kept_lines), 3)           # 4 行删 1 行
            # 清空：整文件删除；再清一次（文件已不存在）也 ok
            self.assertTrue(app.remove_subagents(None)["ok"])
            self.assertFalse(path.exists())
            self.assertTrue(app.remove_subagents([])["ok"])
            self.assertEqual(app.subagents()["items"], [])


class ReasoningReplayTests(unittest.TestCase):
    """chat_loop._attach_reasoning：思考过程附到最后一条 assistant 消息（回放用）。"""

    @staticmethod
    def _load_chat_loop():
        import types

        path = (Path(__file__).resolve().parent.parent
                / "harness" / "builtins" / "chat_loop" / "register.py")
        # 与 loader 同款：直接 exec 源码，绕开 __pycache__ 旧缓存
        mod = types.ModuleType("chat_loop_replay_test")
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), mod.__dict__)
        return mod

    def test_attach_reasoning_marks_last_assistant(self):
        mod = self._load_chat_loop()
        history = [{"role": "user", "content": "问题"},
                   {"role": "assistant", "content": "回答"}]
        mod._attach_reasoning(history, "先查文件再回答")
        self.assertEqual(history[-1]["reasoning"], "先查文件再回答")
        # 空 reasoning 不动历史
        history2 = [{"role": "assistant", "content": "答"}]
        mod._attach_reasoning(history2, "")
        self.assertNotIn("reasoning", history2[0])
        mod._attach_reasoning(history2, None)
        self.assertNotIn("reasoning", history2[0])
        # 没有 assistant 消息（异常轮）不崩
        history3 = [{"role": "user", "content": "问"}]
        mod._attach_reasoning(history3, "思考")
        self.assertNotIn("reasoning", history3[0])


class Batch39ApisTests(unittest.TestCase):
    """第二批功能 API：代码应用 / @引用 / 知识库试检索 / 定时任务面板。"""

    def _app(self, tmp: Path):
        from harness.desktop import DesktopApp

        with patch_model_build():
            host = build_host(tmp)
        return host, DesktopApp(host)

    def test_ws_files_flat_list_and_text_jail(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            ws = Path(host.workspace)
            (ws / "src").mkdir()
            (ws / "src" / "app.js").write_text("console.log(1)\n", encoding="utf-8")
            (ws / "README.md").write_text("hello\n", encoding="utf-8")
            junk = ws / "node_modules"
            junk.mkdir()
            (junk / "lib.js").write_text("x\n", encoding="utf-8")
            files = app.ws_files()["files"]
            self.assertIn("src/app.js", files)
            self.assertIn("README.md", files)
            self.assertNotIn("node_modules/lib.js", files)   # 忽略目录被跳过
            # 原文读取 + 截断
            r = app.ws_file_text("src/app.js")
            self.assertTrue(r["ok"])
            self.assertEqual(r["text"].replace("\r\n", "\n"), "console.log(1)\n")
            big = app.ws_file_text("README.md", max_chars=2)
            self.assertTrue(big["text"].endswith("字符）"))
            # 路径监狱：越界拒绝
            self.assertFalse(app.ws_file_text("../outside.txt")["ok"])
            self.assertFalse(app.ws_file_text("missing.md")["ok"])

    def test_apply_code_uses_write_gate_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            # 默认 fs=ask 且无窗口 → fail-closed 拒绝
            denied = app.apply_code("new.py", "print(1)\n")
            self.assertFalse(denied["ok"])
            self.assertFalse((Path(host.workspace) / "new.py").exists())
            # 放行后可写（含自动建父目录）
            allow_fs(host)
            ok = app.apply_code("src/gen/app.py", "print('hi')\n")
            self.assertTrue(ok["ok"], ok)
            self.assertEqual((Path(host.workspace) / "src" / "gen" / "app.py")
                             .read_text(encoding="utf-8"), "print('hi')\n")
            # 越界路径被 write_file 门拒绝
            outside = app.apply_code("../escape.py", "x")
            self.assertFalse(outside["ok"])

    def test_knowledge_query_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            self.assertFalse(app.knowledge_query("")["ok"])   # 空查询
            r = app.knowledge_query("任何问题")
            self.assertTrue(r["ok"])
            self.assertIsInstance(r["output"], str)

    def test_schedules_list_toggle_remove(self):
        from harness.schedule_store import add_task, load_tasks

        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            add_task(host.profile, "日报", 3600, "总结今天的进展")
            tasks = app.schedules()["tasks"]
            self.assertEqual(len(tasks), 1)
            self.assertTrue(tasks[0]["enabled"])
            # 暂停 / 启用
            self.assertTrue(app.schedule_toggle("日报", False)["ok"])
            self.assertFalse(load_tasks(host.profile)[0]["enabled"])
            self.assertTrue(app.schedule_toggle("日报", True)["ok"])
            self.assertTrue(load_tasks(host.profile)[0]["enabled"])
            # 移除与缺失名
            self.assertTrue(app.schedule_remove("日报")["ok"])
            self.assertEqual(app.schedules()["tasks"], [])
            self.assertFalse(app.schedule_remove("日报")["ok"])
            self.assertFalse(app.schedule_toggle("不存在", True)["ok"])


class Batch40ApisTests(unittest.TestCase):
    """多模型并答对比 + 会话分支。"""

    def test_build_agent_with_isolated_no_tools(self):
        from nanoagent.llm import LLMResponse

        from test_harness import FakeLLM

        with tempfile.TemporaryDirectory() as tmp:
            with patch_model_build():
                host = build_host(Path(tmp))
            build = host.service("build_agent_with")
            self.assertTrue(callable(build))
            agent = build(FakeLLM(), "cmp-x", tools=[])
            self.assertEqual(agent.tools.names(), [])   # 纯对话：无工具
            self.assertIn("当前工作区", agent.instructions)   # 复用主指令装配
            agent.llm.script = [LLMResponse(content="独立回答")]
            result = agent.run("问题", session_id="cmp-x")
            self.assertEqual(result.content, "独立回答")

    def test_compare_models_runs_both(self):
        from nanoagent.llm import LLMResponse

        with tempfile.TemporaryDirectory() as tmp:
            with patch_model_build():
                profile = make_profile(Path(tmp), models=[
                    {"name": "m1", "provider": "openai", "model": "x", "api_key": "k"},
                    {"name": "m2", "provider": "openai", "model": "y", "api_key": "k"},
                ])
                from harness.cli import build_runtime

                host = build_runtime(profile, str(tmp))
            from harness.desktop import DesktopApp

            app = DesktopApp(host)
            runtime = host.service("models_runtime")
            runtime["pool"]["m1"].script = [LLMResponse(content="甲答")]
            runtime["pool"]["m2"].script = [LLMResponse(content="乙答")]
            res = app.compare_models("同一个问题")
            self.assertTrue(res["ok"], res.get("error"))
            self.assertEqual(res["a"]["name"], "m1")
            self.assertEqual(res["a"]["reply"], "甲答")
            self.assertEqual(res["b"]["name"], "m2")
            self.assertEqual(res["b"]["reply"], "乙答")
            # 对比不落会话：profile 里没有 cmp-* 会话文件
            self.assertEqual([s for s in host.profile.session_ids() if s.startswith("cmp-")], [])
            # 单边脚本用尽走 FakeLLM 兜底，不会失败——这里只验证异常路径：
            bad = app.compare_models("问题", "m1", "ghost")
            self.assertFalse(bad["ok"])

    def test_compare_models_needs_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch_model_build():
                host = build_host(Path(tmp))   # 只有 m1
            from harness.desktop import DesktopApp

            app = DesktopApp(host)
            res = app.compare_models("问题")
            self.assertFalse(res["ok"])
            self.assertIn("两个", res["error"])
            self.assertFalse(app.compare_models("")["ok"])   # 空消息

    def test_branch_session_copies_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app_titled_off(Path(tmp))
            app.chat("一", "t1")
            app.chat("二", "t1")
            self.assertEqual(len(app.session_history("t1")["history"]), 4)
            r = app.branch_session("t1", 1)   # 从第 2 条可见消息处（含）分叉
            self.assertTrue(r["ok"], r.get("error"))
            branched = app.session_history(r["session"])["history"]
            self.assertTrue(str(branched[-1]["content"]).endswith("假回答"))
            self.assertEqual(len(branched), 2)
            # 原会话不动；新会话命名 = 原名 + ⎇
            self.assertEqual(len(app.session_history("t1")["history"]), 4)
            meta = host.profile.load_config()["sessions_meta"]
            self.assertTrue(meta[r["session"]]["name"].endswith("⎇"))
            # 越界
            self.assertFalse(app.branch_session("t1", 99)["ok"])

    @staticmethod
    def _app_titled_off(tmp: Path):
        from harness.desktop import DesktopApp

        with patch_model_build():
            host = build_host(tmp)
        host.profile.update_config(auto_title=False)
        return host, DesktopApp(host)


class Batch41ApisTests(unittest.TestCase):
    """第三批：长期记忆 / 改动时间线 diff / hooks / 预算 / 备份恢复。"""

    def _app(self, tmp: Path, titled_off: bool = True):
        from harness.desktop import DesktopApp

        with patch_model_build():
            host = build_host(tmp)
        if titled_off:
            host.profile.update_config(auto_title=False)   # 隔离起名，避免干扰断言
        return host, DesktopApp(host)

    def test_memory_roundtrip_and_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            self.assertEqual(app.agent_memory()["text"], "")
            app.save_agent_memory("本项目用 pytest，提交信息用中文")
            self.assertTrue((Path(host.profile.root) / "memory.md").is_file())
            # 注入：重建的 agent 指令里带记忆文本
            agent = host.service("agent_factory")()
            self.assertIn("本项目用 pytest，提交信息用中文", agent.instructions)
            self.assertIn("长期记忆", agent.instructions)

    def test_checkpoint_diff_shows_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            allow_fs(host)
            ws = Path(host.workspace)
            (ws / "note.txt").write_text("v1\n", encoding="utf-8")
            app.apply_code("note.txt", "v2\n")          # 留底：v1
            r = app.checkpoints(5)
            items = r["items"]
            self.assertTrue(items, "apply_code 应产生检查点")
            d = app.checkpoint_diff(items[0]["id"])
            self.assertTrue(d["ok"])
            self.assertIn("改动前", d["diff"])
            self.assertIn("+v2", d["diff"])
            self.assertIn("note.txt", d["diff"])

    def test_hooks_appended_to_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            host.profile.update_config(hooks={"reply_end": ["echo hook-ok"]})
            res = app.chat("你好", "t1")
            self.assertTrue(res["ok"])
            self.assertIn("[自动钩子]", res["reply"])
            self.assertIn("hook-ok", res["reply"])
            # 无 hooks 配置 → 不附加
            host.profile.update_config(hooks={})
            res2 = app.chat("再来", "t1")
            self.assertNotIn("[自动钩子]", res2["reply"])

    def test_usage_budget_warning(self):
        import json as _json
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            host.profile.update_config(usage_budget=100)
            rec = {"ts": _time.time(), "session": "t1", "model": "m1",
                   "prompt_tokens": 90, "completion_tokens": 20}
            path = host.profile.root / "usage.jsonl"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(rec) + "\n")
            res = app.chat("你好", "t1")
            self.assertIn("budget_warning", res)
            self.assertIn("超预算", res["budget_warning"])
            # 无预算配置 → 无告警
            host.profile.update_config(usage_budget=0)
            res2 = app.chat("再来", "t1")
            self.assertNotIn("budget_warning", res2)

    def test_export_import_profile_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
            app.chat("历史消息", "keep-me")
            app.save_agent_memory("记住这件事")
            exported = app.export_profile("")
            self.assertTrue(exported["ok"])
            self.assertTrue(Path(exported["path"]).is_file())
            # 恢复到一个全新的 profile
            other_root = Path(tmp) / "other-profile"
            from harness.config import Profile
            from harness.desktop import DesktopApp
            from harness.kernel import Harness

            other = Profile(other_root)
            host2 = Harness()
            host2.profile = other
            host2.workspace = str(tmp)
            app2 = DesktopApp(host2)
            r = app2.import_profile(exported["path"])
            self.assertTrue(r["ok"], r.get("error"))
            self.assertGreater(r["restored"], 0)
            self.assertTrue((other_root / "sessions" / "keep-me.json").is_file())
            self.assertEqual((other_root / "memory.md").read_text(encoding="utf-8"),
                             "记住这件事")
            self.assertTrue((other_root / "config.pre-import.json").is_file())
            # 路径穿越成员被跳过
            import zipfile

            evil = Path(tmp) / "evil.zip"
            with zipfile.ZipFile(evil, "w") as zf:
                zf.writestr("../../evil.txt", "x")
                zf.writestr("unknown/thing.bin", "y")
            r2 = app2.import_profile(str(evil))
            self.assertTrue(r2["ok"])
            self.assertEqual(r2["restored"], 0)
            self.assertFalse((other_root.parent / "evil.txt").exists())


class Batch43ApisTests(unittest.TestCase):
    """辅助对话选模型：ask_with 指定模型跑单轮，会话照常落盘。"""

    def test_ask_with_custom_model(self):
        from nanoagent.llm import LLMResponse

        with tempfile.TemporaryDirectory() as tmp:
            with patch_model_build():
                profile = make_profile(Path(tmp), models=[
                    {"name": "m1", "provider": "openai", "model": "x", "api_key": "k"},
                    {"name": "m2", "provider": "openai", "model": "y", "api_key": "k"},
                ])
                from harness.cli import build_runtime

                host = build_runtime(profile, str(tmp))
            from harness.desktop import DesktopApp

            app = DesktopApp(host)
            host.service("models_runtime")["pool"]["m2"].script = [
                LLMResponse(content="乙答")]
            # 指定 m2（≠当前 m1）→ 一次性 agent，回复与模型名来自 m2
            res = app.chat("问题", "aux", None, "m2")
            self.assertTrue(res["ok"], res.get("error"))
            self.assertEqual(res["reply"], "乙答")
            self.assertEqual(res["model"], "m2")
            # 会话照常落盘（含本轮问答）
            history = app.session_history("aux")["history"]
            self.assertEqual(history[-1]["content"], "乙答")
            # 不存在的模型 → 可读错误
            bad = app.chat("问题", "aux", None, "ghost")
            self.assertFalse(bad["ok"])
            self.assertIn("ghost", bad["error"])


if __name__ == "__main__":
    unittest.main()
