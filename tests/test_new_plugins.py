"""功能4-7 的回归测试：通知 webhook / 定时任务 / fetch_url / 多模态图片输入。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nanoagent.llm import LLMResponse
from test_harness import build_host, make_profile, patch_model_build, tool

from harness.cli import build_runtime
from harness.desktop import DesktopApp
from harness.schedule_store import ScheduleError, add_task, due_tasks, load_tasks
from harness.workspace import safe_image, safe_images


# ----------------------------------------------------------------------
# 本地 HTTP 服务（通知 webhook / fetch_url 共用）
# ----------------------------------------------------------------------
class _Recorder(BaseHTTPRequestHandler):
    """把收到的请求记到类属性，供测试断言。"""

    posts: ClassVar[list[dict]] = []
    gets: ClassVar[list[str]] = []
    body: ClassVar[bytes] = b""
    status: ClassVar[int] = 200
    content_type: ClassVar[str] = "text/html"

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length)
        try:
            type(self).posts.append(json.loads(raw))
        except ValueError:
            type(self).posts.append({"_raw": raw.decode("utf-8", "replace")})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self) -> None:
        type(self).gets.append(self.path)
        self.send_response(type(self).status)
        self.send_header("content-type", type(self).content_type)
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *args) -> None:  # 静默
        pass


def _start_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ----------------------------------------------------------------------
# 功能4：notifications 插件
# ----------------------------------------------------------------------
class NotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="sha-notify-"))

    def test_not_configured_plugin_absent(self) -> None:
        host = build_host(self.tmp)
        self.addCleanup(host.shutdown)
        record = host.plugins["notifications"]
        self.assertTrue(any("not-configured" in note for note in record.skipped))
        self.assertFalse(any(t.name == "notify" for t in host.collect_tools()))

    def test_notify_posts_json_to_webhook(self) -> None:
        server = _start_server()
        self.addCleanup(server.shutdown)
        _Recorder.posts.clear()
        profile = make_profile(self.tmp)
        profile.update_config(notifications={"url": f"http://127.0.0.1:{server.server_port}/hook"})
        host = build_runtime(profile, str(self.tmp))
        self.addCleanup(host.shutdown)

        record = host.plugins["notifications"]
        self.assertEqual(record.state, "ACTIVE")
        notify = next(t for t in host.collect_tools() if t.name == "notify")
        result = notify.invoke({"title": "标题", "message": "正文内容"})
        self.assertIn("已通知", str(result))
        self.assertEqual(len(_Recorder.posts), 1)
        payload = _Recorder.posts[0]
        self.assertEqual(payload["title"], "标题")
        self.assertEqual(payload["message"], "正文内容")
        self.assertEqual(payload["source"], "sahou-harness")

    def test_error_event_pushes_webhook(self) -> None:
        server = _start_server()
        self.addCleanup(server.shutdown)
        _Recorder.posts.clear()
        profile = make_profile(self.tmp)
        profile.update_config(notifications={"url": f"http://127.0.0.1:{server.server_port}/hook",
                                             "on_error": True})
        host = build_runtime(profile, str(self.tmp))
        self.addCleanup(host.shutdown)
        host.bus.emit("error", name="某插件", error="boom")
        deadline = time.time() + 5
        while not _Recorder.posts and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(any("某插件" in str(p.get("message", "")) for p in _Recorder.posts))


# ----------------------------------------------------------------------
# 功能5：scheduler 插件
# ----------------------------------------------------------------------
class ScheduleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="sha-sched-store-"))
        self.profile = make_profile(self.tmp)

    def test_add_and_validate(self) -> None:
        task = add_task(self.profile, "日报", 60, "总结进展")
        self.assertEqual(task["name"], "日报")
        self.assertEqual(load_tasks(self.profile)[0]["prompt"], "总结进展")
        with self.assertRaises(ScheduleError):  # 重名
            add_task(self.profile, "日报", 60, "again")
        with self.assertRaises(ScheduleError):  # 非法名字（路径分隔符）
            add_task(self.profile, "a/b", 60, "x")
        with self.assertRaises(ScheduleError):  # 间隔过小
            add_task(self.profile, "ok名", 0, "x")
        with self.assertRaises(ScheduleError):  # 空 prompt
            add_task(self.profile, "ok名2", 60, "  ")

    def test_remove(self) -> None:
        add_task(self.profile, "t1", 60, "p")
        from harness.schedule_store import remove_task

        self.assertTrue(remove_task(self.profile, "t1"))
        self.assertFalse(remove_task(self.profile, "t1"))

    def test_pause_and_resume(self) -> None:
        from harness.schedule_store import set_task_enabled

        add_task(self.profile, "暂停我", 10, "p")
        # 暂停 → enabled 翻 False，due_tasks 不再挑出
        self.assertTrue(set_task_enabled(self.profile, "暂停我", enabled=False))
        now = time.time()
        tasks = load_tasks(self.profile)
        self.assertFalse(tasks[0]["enabled"])
        self.assertEqual(due_tasks(tasks, now), [])
        # 恢复 → 重新可见
        self.assertTrue(set_task_enabled(self.profile, "暂停我", enabled=True))
        tasks = load_tasks(self.profile)
        self.assertTrue(tasks[0]["enabled"])
        self.assertEqual([t["name"] for t in due_tasks(tasks, now)], ["暂停我"])
        # 不存在的任务
        self.assertFalse(set_task_enabled(self.profile, "不存在", enabled=True))

    def test_due_tasks(self) -> None:
        now = time.time()
        tasks = [
            {"name": "到期", "every": 10, "prompt": "p", "enabled": True, "last_run": now - 11},
            {"name": "未到", "every": 100, "prompt": "p", "enabled": True, "last_run": now - 5},
            {"name": "停用", "every": 1, "prompt": "p", "enabled": False, "last_run": 0},
            {"name": "坏间隔", "every": "abc", "prompt": "p", "enabled": True, "last_run": 0},
        ]
        names = [t["name"] for t in due_tasks(tasks, now)]
        self.assertEqual(names, ["到期"])


class KnowledgeServiceTests(unittest.TestCase):
    """知识库插件以 host.service("knowledge") 暴露给桌面端的能力（不联网，懒构建）。"""

    def test_status_clear_and_abs_path_guard(self) -> None:
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="sha-kb-"))
        profile = make_profile(tmp)
        with patch_model_build():
            host = build_runtime(profile, str(tmp))
            self.addCleanup(host.shutdown)
            svc = host.service("knowledge")
            self.assertIsInstance(svc, dict)
            # 空库状态（不要求模型可用）
            info = svc["status"]()
            self.assertEqual(info["chunks"], 0)
            self.assertEqual(info["backend"], "numpy")
            # 不存在的绝对路径 → 可读错误
            self.assertIn("错误", svc["index_absolute"](str(tmp / "no-such-dir")))
            # 清空：索引目录还没有文件也应当成功
            summary = svc["clear"]()
            self.assertIn("已清空", summary)
            self.assertEqual(svc["status"]()["chunks"], 0)


class SchedulerPluginTests(unittest.TestCase):
    def test_interval_task_runs_and_writes_log(self) -> None:
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="sha-sched-run-"))
        profile = make_profile(tmp)
        add_task(profile, "e2e", 1, "报一声到岗")
        with patch_model_build():
            host = build_runtime(profile, str(tmp))
            self.addCleanup(host.shutdown)
            self.assertEqual(host.plugins["scheduler"].state, "ACTIVE")

            log = profile.root / "scheduled" / "e2e.log"
            # 轮询到日志内容出现（文件先创建、内容后写入，不能只看 exists()）
            content = ""
            deadline = time.time() + 15
            while time.time() < deadline:
                if log.exists():
                    try:
                        content = log.read_text(encoding="utf-8")
                    except OSError:  # 写入中 temporarily 不可读，继续等
                        content = ""
                    if "完成" in content:
                        break
                time.sleep(0.2)
            self.assertIn("完成", content)
            # 任务状态（last_run / runs）与日志写入异步，同样轮询
            deadline = time.time() + 10
            while time.time() < deadline:
                tasks = load_tasks(profile)
                if tasks[0]["last_run"] > 0 and tasks[0]["runs"]:
                    break
                time.sleep(0.2)
            tasks = load_tasks(profile)
            self.assertGreater(tasks[0]["last_run"], 0)
            self.assertTrue(tasks[0]["runs"])


# ----------------------------------------------------------------------
# 功能6：browser 插件
# ----------------------------------------------------------------------
PAGE_HTML = (
    "<html><head><style>.x { color: red; }</style>"
    "<script>var secret_script = 42;</script></head>"
    "<body><h1>标题行</h1><p>正文第一段 &amp; 符号</p>"
    "<p>正文第一段 &amp; 符号</p></body></html>"
)


class BrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="sha-browser-"))
        _Recorder.body = PAGE_HTML.encode("utf-8")
        _Recorder.status = 200
        _Recorder.content_type = "text/html; charset=utf-8"
        _Recorder.gets.clear()
        self.server = _start_server()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()

    def _host(self, **config):
        profile = make_profile(self.tmp)
        for key, value in config.items():
            profile.update_config(**{key: value})
        host = build_runtime(profile, str(self.tmp))
        self.addCleanup(host.shutdown)
        return next(t for t in host.collect_tools() if t.name == "fetch_url")

    def test_fetch_html_to_text(self) -> None:
        fetch = self._host()
        result = fetch.invoke({"url": f"{self.base}/page"})
        self.assertIn("标题行", str(result))
        self.assertIn("正文第一段 & 符号", str(result))  # 实体已解码
        self.assertNotIn("secret_script", str(result))   # script 已剔除
        self.assertNotIn("<p>", str(result))             # 标签已剔除
        # 相邻重复行折叠
        self.assertEqual(str(result).count("正文第一段 & 符号"), 1)

    def test_rejects_non_http_scheme(self) -> None:
        fetch = self._host()
        result = str(fetch.invoke({"url": "file:///C:/Windows/win.ini"}))
        self.assertIn("错误", result)
        self.assertIn("http", result)

    def test_http_error_readable(self) -> None:
        _Recorder.status = 404
        fetch = self._host()
        result = str(fetch.invoke({"url": f"{self.base}/missing"}))
        self.assertIn("404", result)

    def test_domain_whitelist(self) -> None:
        fetch = self._host(browser={"allowed_domains": ["example.com"]})
        result = str(fetch.invoke({"url": f"{self.base}/page"}))
        self.assertIn("白名单", result)
        self.assertIn("127.0.0.1", result)


# ----------------------------------------------------------------------
# 功能7：多模态图片输入
# ----------------------------------------------------------------------
class MultimodalTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="sha-mm-"))
        from nanoagent.media import make_png

        self.png = make_png(4, 4, (200, 30, 30), self.tmp / "pic.png")

    def test_safe_image_prison(self) -> None:
        profile = make_profile(self.tmp)
        host = build_runtime(profile, str(self.tmp))
        self.addCleanup(host.shutdown)
        # 工作区内图片 → 返回绝对路径
        self.assertEqual(safe_image(host, "pic.png"), str(self.png.resolve()))
        # 工作区外 → 拒绝
        outside = self.tmp.resolve().parent / "outside.png"
        outside.write_bytes(b"x")
        with self.assertRaises(ValueError):
            safe_image(host, str(outside))
        # 不像图片的扩展名 → 拒绝
        (self.tmp / "note.txt").write_text("hi", encoding="utf-8")
        with self.assertRaises(ValueError):
            safe_image(host, "note.txt")
        # URL 放行
        self.assertEqual(safe_image(host, "https://x.test/a.png"), "https://x.test/a.png")
        # 空列表 → None（保持「无图」语义）
        self.assertIsNone(safe_images(host, None))
        self.assertIsNone(safe_images(host, []))

    def test_ask_builds_multimodal_message(self) -> None:
        import nanoagent

        holder: dict = {}

        class CapturingLLM:
            model = "fake"
            total_usage: ClassVar[dict] = {"prompt_tokens": 1, "completion_tokens": 1}

            def chat(self, messages, tools=None):
                holder["messages"] = messages
                return LLMResponse(content="看到图片了")

        original = nanoagent.LLM
        nanoagent.LLM = lambda **kwargs: CapturingLLM()
        try:
            profile = make_profile(self.tmp)
            host = build_runtime(profile, str(self.tmp))
            self.addCleanup(host.shutdown)
            ask = host.service("ask")
            result = ask("描述这张图", images=["pic.png"])
        finally:
            nanoagent.LLM = original
        self.assertEqual(result["reply"], "看到图片了")
        user_msg = [m for m in holder["messages"] if m["role"] == "user"][-1]
        parts = user_msg["content"]
        self.assertIsInstance(parts, list)
        self.assertEqual(parts[0], {"type": "text", "text": "描述这张图"})
        url = parts[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))

    def test_ask_rejects_out_of_workspace_image(self) -> None:
        import tempfile

        outside_dir = Path(tempfile.mkdtemp(prefix="sha-mm-outside-"))
        outside = outside_dir / "o.png"
        outside.write_bytes(b"x")
        profile = make_profile(self.tmp)
        host = build_runtime(profile, str(self.tmp))
        self.addCleanup(host.shutdown)
        ask = host.service("ask")
        with self.assertRaises(ValueError) as ctx:
            ask("看图", images=[str(outside)])
        self.assertIn("越出工作区", str(ctx.exception))

    def test_ask_url_passthrough(self) -> None:
        import nanoagent

        holder: dict = {}

        class CapturingLLM:
            model = "fake"
            total_usage: ClassVar[dict] = {"prompt_tokens": 1, "completion_tokens": 1}

            def chat(self, messages, tools=None):
                holder["messages"] = messages
                return LLMResponse(content="ok")

        original = nanoagent.LLM
        nanoagent.LLM = lambda **kwargs: CapturingLLM()
        try:
            profile = make_profile(self.tmp)
            host = build_runtime(profile, str(self.tmp))
            self.addCleanup(host.shutdown)
            host.service("ask")("看", images=["https://x.test/a.png"])
        finally:
            nanoagent.LLM = original
        user_msg = [m for m in holder["messages"] if m["role"] == "user"][-1]
        self.assertEqual(user_msg["content"][1]["image_url"]["url"], "https://x.test/a.png")


if __name__ == "__main__":
    unittest.main()


class ToolsGitTests(unittest.TestCase):
    """F4：受限 git 工具——只读三件套免门，add/commit 走权限门，测试门可拒绝提交。"""

    def _repo_host(self, tmp: Path):
        import subprocess

        with patch_model_build():
            host = build_host(Path(tmp))
        ws = Path(host.workspace)
        ws.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=ws, check=False)
        # 有一个已跟踪文件，diff 才有内容（untracked 不进 git diff）
        (ws / "README.md").write_text("demo", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=ws, check=False)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "基线"], cwd=ws, check=False)
        return host, ws

    def test_readonly_tools_and_commit_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, ws = self._repo_host(Path(tmp))
            status = tool(host, "git_status")
            diff = tool(host, "git_diff")
            log = tool(host, "git_log")
            add = tool(host, "git_add")
            commit = tool(host, "git_commit")

            self.assertIn("分支:", status.invoke({}))
            self.assertIn("基线", log.invoke({}))
            (ws / "README.md").write_text("changed", encoding="utf-8")
            self.assertIn("README.md", diff.invoke({}))

            # F4 修订：提交默认由用户手动完成——agent 调 add/commit 直接被拒
            self.assertIn("由用户手动完成", add.invoke({"paths": "README.md"}))
            self.assertIn("由用户手动完成", commit.invoke({"message": "更新 README"}))
            # 开放 agent 提交后：暂存区为空的检查先于权限确认（不打扰）
            host.profile.update_config(git={"allow_agent_commit": True})
            out = commit.invoke({"message": "更新 README"})
            self.assertIn("暂存区为空", out)

            def allow_confirm(prompt: str) -> bool:
                return True

            host.confirm = allow_confirm
            self.assertIn("已暂存", add.invoke({"paths": "README.md"}))
            # 测试门：配置了 test_command 且失败 → 拒绝提交
            host.profile.update_config(
                git={"allow_agent_commit": True, "test_command": "exit 1"})
            gated = commit.invoke({"message": "更新 README"})
            self.assertIn("测试门未通过", gated)
            # 测试门通过 → 提交成功
            host.profile.update_config(
                git={"allow_agent_commit": True, "test_command": "exit 0"})
            self.assertIn("已提交", commit.invoke({"message": "更新 README"}))
            self.assertIn("更新 README", log.invoke({"count": 1}))

    def test_deny_blocks_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, _ws = self._repo_host(Path(tmp))
            host.profile.update_config(permissions={"git": "deny"})
            self.assertIn("deny", tool(host, "git_status").invoke({}))
            self.assertIn("deny", tool(host, "git_add").invoke({"paths": "."}))

    def test_agent_commit_disabled_by_default_even_allow(self):
        """F4 修订：即使 permissions.git=allow，agent 提交默认仍关闭（用户手动）。"""
        with tempfile.TemporaryDirectory() as tmp:
            host, _ws = self._repo_host(Path(tmp))
            host.profile.update_config(permissions={"git": "allow"})
            self.assertIn("由用户手动完成",
                          tool(host, "git_add").invoke({"paths": "."}))
            self.assertIn("由用户手动完成",
                          tool(host, "git_commit").invoke({"message": "x"}))


class BootstrapTests(unittest.TestCase):
    """F5：工程脚手架——只增量创建；git 基线提交；目标写入 README。"""

    def test_run_bootstrap_creates_and_commits(self):
        import subprocess

        from harness.bootstrap import run_bootstrap

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "proj"
            ws.mkdir()
            res = run_bootstrap(ws, "做一个待办应用")
            self.assertTrue(res["ok"], res.get("error"))
            self.assertTrue(res["committed"])
            for name in ("src", "tests", "docs", ".gitignore"):
                self.assertTrue((ws / name).exists(), name)
            self.assertIn("待办应用", (ws / "README.md").read_text(encoding="utf-8"))
            log = subprocess.run(["git", "log", "--oneline"], cwd=ws,
                                 capture_output=True, check=False)
            self.assertIn("做一个待办应用", log.stdout.decode("utf-8", "replace"))
            # 重复执行：不覆盖、不报错
            res2 = run_bootstrap(ws, "另一个目标")
            self.assertTrue(res2["ok"])
            self.assertEqual(res2["created"], [])
            # README 已存在不会被第二个目标覆盖
            self.assertIn("待办应用", (ws / "README.md").read_text(encoding="utf-8"))


class SubagentReadonlyTests(unittest.TestCase):
    """F7：subagent.readonly=true 时剥离写类工具。"""

    def test_readonly_filters_write_tools(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            host.profile.update_config(subagent={"readonly": True})
            agent = host.service("subagent_factory")()
            names = set(agent.tools.names())
            self.assertIn("read_file", names)
            self.assertNotIn("write_file", names)
            self.assertNotIn("run_command", names)


class BundledSkillsSeedTests(unittest.TestCase):
    """exe 内置技能池播种进 profile：补缺失、不覆盖已有、失败静默。"""

    @staticmethod
    def _make_skill(root: Path, name: str, body: str) -> None:
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} 技能\n---\n{body}",
            encoding="utf-8")
        (d / "ref.txt").write_text(f"ref of {name}", encoding="utf-8")

    def test_seed_copies_missing_keeps_existing_idempotent(self):
        from harness.builtins.skills.register import seed_bundled_skills

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundled = tmp / "bundled"
            self._make_skill(bundled, "alpha", "A")
            self._make_skill(bundled, "beta", "B")
            profile = tmp / "profile" / "skills"
            self._make_skill(profile, "beta", "用户已改过的 beta")

            seeded = seed_bundled_skills(profile, bundled_root=bundled)
            self.assertEqual(seeded, ["alpha"])
            # alpha 整目录落地（含附属文件）
            self.assertTrue((profile / "alpha" / "SKILL.md").exists())
            self.assertEqual(
                (profile / "alpha" / "ref.txt").read_text(encoding="utf-8"),
                "ref of alpha")
            # 已存在的 beta 原样保留
            self.assertIn(
                "用户已改过的 beta",
                (profile / "beta" / "SKILL.md").read_text(encoding="utf-8"))
            # 幂等：再次播种无事发生
            self.assertEqual(seed_bundled_skills(profile, bundled_root=bundled), [])

    def test_seed_missing_root_is_noop(self):
        from harness.builtins.skills.register import seed_bundled_skills

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "skills"
            self.assertEqual(
                seed_bundled_skills(profile, bundled_root=Path(tmp) / "nope"), [])
            self.assertFalse(profile.exists())

    def test_register_seeds_before_add_dir(self):
        """register(ctx)：先播种再声明目录，同一次启动即可见新技能。"""
        from harness.builtins.skills.register import register as register_skills

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._make_skill(tmp / "bundled_skills", "gamma", "G")
            profile_root = tmp / "profile"

            added: list[str] = []

            class _FakeSkills:
                def add_dir(self, path):
                    added.append(path)

            class _Ctx:
                host = type("H", (), {"profile": type(
                    "P", (), {"root": str(profile_root)})()})()
                skills = _FakeSkills()

            # 模拟 PyInstaller 冻结环境：_MEIPASS 指向解包根
            had_meipass = hasattr(sys, "_MEIPASS")
            old_meipass = getattr(sys, "_MEIPASS", None)
            sys._MEIPASS = str(tmp)
            try:
                register_skills(_Ctx())
            finally:
                if had_meipass:
                    sys._MEIPASS = old_meipass
                else:
                    del sys._MEIPASS

            self.assertEqual(added, [str(profile_root / "skills")])
            self.assertTrue(
                (profile_root / "skills" / "gamma" / "SKILL.md").exists())


class MaxIterationsTests(unittest.TestCase):
    """工具调用轮数上限：默认 10、可配置、非法值回退、范围钳制。"""

    def test_max_iterations_config(self):
        from harness.builtins.chat_loop.register import _max_iterations

        self.assertEqual(_max_iterations({}), 10)
        self.assertEqual(_max_iterations({"max_iterations": 25}), 25)
        self.assertEqual(_max_iterations({"max_iterations": "40"}), 40)
        self.assertEqual(_max_iterations({"max_iterations": "abc"}), 10)
        self.assertEqual(_max_iterations({"max_iterations": 0}), 1)
        self.assertEqual(_max_iterations({"max_iterations": 99999}), 200)


class ScheduleBackflowTests(unittest.TestCase):
    """定时任务结果回流：session 字段、set_task_session、write_back_result。"""

    def test_add_task_with_session_and_set_session(self):
        from harness.schedule_store import (add_task, load_tasks, remove_task,
                                            set_task_session)

        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp))
            try:
                task = add_task(profile, "日报", 3600, "总结进展", session="s-1")
                self.assertEqual(task["session"], "s-1")
                self.assertTrue(set_task_session(profile, "日报", "s-2"))
                self.assertEqual(load_tasks(profile)[0]["session"], "s-2")
                self.assertTrue(set_task_session(profile, "日报", ""))  # 清空回流
                self.assertEqual(load_tasks(profile)[0]["session"], "")
                self.assertFalse(set_task_session(profile, "不存在", "s-9"))
            finally:
                remove_task(profile, "日报")

    def test_write_back_result(self):
        from harness.schedule_store import remove_task, write_back_result

        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp))
            try:
                task = {"name": "巡检", "session": "s-back"}
                # 目标会话不存在时 load 返回空历史 → 追加后落盘
                out = write_back_result(profile, task, "一切正常")
                self.assertEqual(out, {"session": "s-back"})
                history = profile.load_session("s-back")
                self.assertEqual([m["role"] for m in history], ["user", "assistant"])
                self.assertIn("巡检", history[0]["content"])
                self.assertEqual(history[1]["content"], "一切正常")
                # 未配置 session → None
                self.assertIsNone(
                    write_back_result(profile, {"name": "x", "session": ""}, "hi"))
                # 目标会话正在回答（busy）→ 跳过，不落盘
                before = len(profile.load_session("s-back"))
                out = write_back_result(profile, task, "第二条", is_busy=lambda s: True)
                self.assertIsNone(out)
                self.assertEqual(len(profile.load_session("s-back")), before)
            finally:
                remove_task(profile, "巡检")


class NewFeatureApiTests(unittest.TestCase):
    """第五十一批桌面端 API：技能开关 / 回流参数 / 自动压缩 / trace / 本地模型护栏。"""

    def _app(self, tmp):
        host = build_host(Path(tmp))
        return host, DesktopApp(host)

    def test_skill_toggle_flow(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            host, app = self._app(tmp)
            # 造一个 profile 技能
            skill_dir = Path(tmp) / "my-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: 测试技能\n---\n正文", encoding="utf-8")
            self.assertTrue(app.install_skill(str(skill_dir))["ok"])
            names = {s["name"] for s in app.skills()["skills"]}
            self.assertIn("my-skill", names)
            # 禁用 → 列表带 disabled 标志 + config 落盘
            self.assertTrue(app.set_skill_enabled("my-skill", False)["ok"])
            listing = {s["name"]: s for s in app.skills()["skills"]}
            self.assertTrue(listing["my-skill"]["disabled"])
            cfg = json.loads((Path(host.profile.root) / "config.json").read_text(encoding="utf-8"))
            self.assertIn("my-skill", cfg.get("disabled_skills") or [])
            # 再启用
            self.assertTrue(app.set_skill_enabled("my-skill", True)["ok"])
            listing = {s["name"]: s for s in app.skills()["skills"]}
            self.assertFalse(listing["my-skill"]["disabled"])

    def test_schedule_set_session_via_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app = self._app(tmp)
            self.assertTrue(app.schedule_add("体检", 3600, "报个平安")["ok"])
            self.assertTrue(app.schedule_set_session("体检", "s-9")["ok"])
            tasks = {t["name"]: t for t in app.schedules()["tasks"]}
            self.assertEqual(tasks["体检"]["session"], "s-9")
            self.assertTrue(app.schedule_set_session("体检", "")["ok"])

    def test_auto_compact_toggle(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app = self._app(tmp)
            self.assertFalse(app.auto_compact_enabled()["enabled"])
            self.assertTrue(app.set_auto_compact(True)["ok"])
            self.assertTrue(app.auto_compact_enabled()["enabled"])
            memory = app._config().get("memory") or {}
            self.assertEqual(memory.get("type"), "summary")
            self.assertTrue(app.set_auto_compact(False)["ok"])
            self.assertEqual((app._config().get("memory") or {}).get("type"), "full")

    def test_trace_days_empty_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app = self._app(tmp)
            r = app.trace_days()
            self.assertTrue(r["ok"])
            self.assertEqual(r["days"], [])
            self.assertEqual(app.trace_events("2026-09-30")["events"], [])
            self.assertFalse(app.trace_events("../evil")["ok"])

    def test_probe_local_models_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app = self._app(tmp)
            self.assertFalse(app.probe_local_models("http://example.com/v1")["ok"])
            self.assertFalse(app.probe_local_models("ftp://127.0.0.1:1")["ok"])
            self.assertFalse(app.probe_local_models("")["ok"])

    def test_ask_user_sink_without_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app = self._app(tmp)
            # 无窗口（测试环境）：立即返回提示，绝不阻塞
            out = app._ask_user_sink("选 A 还是 B？", "A|B", timeout=10)
            self.assertIn("没有交互界面", out)
            self.assertFalse(app.answer_ask_user("nope", "x")["ok"])
            # 有等待者时 answer_ask_user 唤醒事件并带回答案
            import threading as _th
            event = _th.Event()
            holder = {"answer": ""}
            app._ask_waiters["au-test"] = (event, holder)
            self.assertTrue(app.answer_ask_user("au-test", "用 B 方案")["ok"])
            self.assertTrue(event.is_set())
            self.assertEqual(holder["answer"], "用 B 方案")
            # 回答一次后等待者被弹出，重复回答报不存在
            self.assertFalse(app.answer_ask_user("au-test", "again")["ok"])
