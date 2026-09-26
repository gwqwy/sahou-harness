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
            host, app = self._app(Path(tmp))
            status = app.status()
            self.assertTrue(status["ok"])
            self.assertEqual(status["model"], "m1")
            self.assertIn("m1", status["models"])
            self.assertEqual(status["thinking_level"], "off")
            self.assertEqual(status["shell_permission"], "ask")
            self.assertEqual(status["fs_permission"], "ask")
            models = app.get_models()
            self.assertEqual(models["default_model"], "m1")

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
            host, app = self._app(Path(tmp))
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
                self.assertIsNone(getattr(llm, "reasoning_effort", None))
            bad = app.set_thinking("ultra")
            self.assertFalse(bad["ok"])

    def test_usage_and_context_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
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
            host, app = self._app(Path(tmp))
            self.assertFalse(app.choose_workspace()["ok"])

    def test_plugin_install_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
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
            host, app = self._app(Path(tmp))
            result = app.remove_plugin("不存在的插件")
            self.assertFalse(result["ok"], result)

    def test_theme_persist_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
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
            host, app = self._app(Path(tmp))
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
            host, app = self._app(Path(tmp))
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
            host, app = self._app(Path(tmp))
            self.assertFalse(app.market_install("http://127.0.0.1/repo")["ok"])
            self.assertFalse(app.market_install("ftp://github.com/a/b")["ok"])
            self.assertFalse(app.market_install("")["ok"])

    def test_market_install_clones_and_installs(self):
        import shutil
        import subprocess
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(Path(tmp))
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
            host, app = self._app(Path(tmp))
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
            host, app = self._app(Path(tmp))
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
            host, app = self._app(Path(tmp))
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


if __name__ == "__main__":
    unittest.main()
