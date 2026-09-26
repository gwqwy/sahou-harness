"""功能4-7 的回归测试：通知 webhook / 定时任务 / fetch_url / 多模态图片输入。"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nanoagent.llm import LLMResponse
from test_harness import build_host, make_profile, patch_model_build

from harness.cli import build_runtime
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
            deadline = time.time() + 15
            while (not log.exists()) and time.time() < deadline:
                time.sleep(0.2)
            self.assertTrue(log.exists(), "15 秒内应看到任务日志落盘")
            content = log.read_text(encoding="utf-8")
            self.assertIn("完成", content)
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
