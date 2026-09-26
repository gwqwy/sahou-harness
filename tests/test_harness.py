"""sahou-harness 测试：内核生命周期、插件装载、模型切换、权限门、端到端对话。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nanoagent.llm import LLMResponse

from harness.config import Profile
from harness.kernel import ACTIVE, DISPOSED, FAILED, Harness

TESTS_DIR = Path(__file__).resolve().parent


class FakeLLM:
    """可脚本化的假模型客户端（真实 LLM 结构的离线替身）。"""

    model = "fake"

    def __init__(self, tag=""):
        self.tag = tag
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.script = []

    def chat(self, messages, tools=None):
        if self.script:
            return self.script.pop(0)
        return LLMResponse(content=f"[{self.tag}] 假回答")


def make_profile(tmp: Path, models=None, default="m1") -> Profile:
    profile = Profile(tmp / ".harness" / "profiles" / "test")
    if models is None:
        models = [{"name": "m1", "provider": "openai", "model": "x", "api_key": "k"}]
    profile.save_config({
        "default_model": default,
        "models": models,
        # 与生产默认一致：两个权限门都默认 ask（fail-closed）。
        # 需要免确认写文件的用例请显式调用 allow_fs(host)。
        "permissions": {"shell": "ask", "fs": "ask"},
    })
    return profile


def allow_fs(host: Harness) -> Harness:
    """把文件写入门放开为 allow —— 供「测路径监狱/工具往返」而非测权限门的用例使用。"""
    host.profile.update_config(permissions={"fs": "allow"})
    return host


def allow_shell(host: Harness) -> Harness:
    """把 shell 门放开为 allow —— 供不关心权限门、只测命令执行链路的用例使用。"""
    host.profile.update_config(permissions={"shell": "allow"})
    return host


def build_host(tmp: Path, **config_overrides) -> Harness:
    from harness.cli import build_runtime

    profile = make_profile(tmp, **config_overrides)
    return build_runtime(profile, str(tmp))


class StreamingFakeLLM(FakeLLM):
    """带 chat_stream 的假模型（真模型结构的最小替身）：用于验证流式路径。"""

    chunks = ("流", "式", "回", "答")

    def chat_stream(self, messages, tools=None):
        from nanoagent.llm import StreamResult

        result = StreamResult()
        result.response = LLMResponse(content="".join(self.chunks))
        return result.bind(iter(self.chunks))


def tool(host, name: str):
    """按名字取出已聚合的工具（不存在直接失败，避免测试静默跑空）。"""
    return next(t for t in host.collect_tools() if t.name == name)


def patch_model_build():
    """把 models 插件的 LLM 构建替换为 FakeLLM（离线测试）。

    models/register.py 在运行期 `from nanoagent import LLM`，因此 patch 包属性即可。
    """
    import nanoagent

    class _Patched:
        def __enter__(self):
            self._orig = nanoagent.LLM
            nanoagent.LLM = lambda **kwargs: FakeLLM(kwargs.get("model", ""))
            return self

        def __exit__(self, *exc):
            nanoagent.LLM = self._orig

    return _Patched()


class KernelTests(unittest.TestCase):
    def _plugin(self, tmp, name: str, body: str, manifest_name: str | None = None,
                **manifest_extra) -> Path:
        """name 同时是目录名；manifest_name 可让清单里的插件名与目录名不同。"""
        tmp = Path(tmp)
        path = tmp / name
        path.mkdir(parents=True, exist_ok=True)
        manifest = {"name": manifest_name or name}
        manifest.update(manifest_extra)
        (path / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        (path / "register.py").write_text(body, encoding="utf-8")
        return path

    def test_mount_activate_dispose_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Harness()
            plugin = self._plugin(tmp, "demo", """
order = []
def register(ctx):
    ctx.provide("svc", 123)
    ctx.provide("log", order)
    order.append("activated")
    ctx.effect(lambda: (lambda: order.append("disposed")))
""")
            host.mount(plugin)
            record = host.activate("demo")
            self.assertEqual(record.state, ACTIVE)
            self.assertEqual(host.service("svc"), 123)
            self.assertEqual(host.service("log"), ["activated"])
            log = host.service("log")  # 持有引用：卸载后服务注销但列表对象仍可断言
            host.deactivate("demo")
            self.assertEqual(record.state, DISPOSED)
            self.assertIsNone(host.service("svc", None))
            self.assertEqual(log, ["activated", "disposed"])

    def test_effect_disposers_run_lifo_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Harness()
            plugin = self._plugin(tmp, "demo", """
order = []
def register(ctx):
    ctx.provide("log", order)
    ctx.effect(lambda: (lambda: order.append("b")))
    ctx.effect(lambda: (lambda: order.append("a")))
""")
            host.mount(plugin)
            host.activate("demo")
            ctx = host.plugins["demo"].ctx
            ctx.deactivate()
            ctx.deactivate()  # 幂等
            self.assertEqual(host.service("log"), ["a", "b"])

    def test_failed_plugin_does_not_block_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Harness()
            host.mount(self._plugin(tmp, "bad", "raise RuntimeError('装不上')"))
            host.mount(self._plugin(tmp, "good", "def register(ctx):\n    ctx.provide('ok', 1)"))
            self.assertEqual(host.activate("bad").state, FAILED)
            self.assertEqual(host.activate("good").state, ACTIVE)
            self.assertEqual(host.service("ok"), 1)

    def test_events_emit(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Harness()
            seen = []
            host.bus.on("activate", lambda **kw: seen.append(kw["name"]))
            host.mount(self._plugin(tmp, "demo", "def register(ctx):\n    pass"))
            host.activate("demo")
            self.assertEqual(seen, ["demo"])

    def test_on_disposer_removes_listener(self):
        """H-04：插件卸载后 ctx.on 订阅的监听器必须从总线摘除（无幽灵监听器）。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = Harness()
            host.mount(self._plugin(tmp, "listener", """
def register(ctx):
    ctx.on("ping", lambda **kw: None)
"""))
            host.activate("listener")
            self.assertEqual(len(host.bus._handlers.get("ping", [])), 1)
            host.deactivate("listener")
            self.assertEqual(len(host.bus._handlers.get("ping", [])), 0)

    def test_deactivate_keeps_other_plugins_services(self):
        """H-05：卸载一个插件不得清掉其它插件提供的服务。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = Harness()
            host.mount(self._plugin(tmp, "a", "def register(ctx):\n    ctx.provide('a', 1)"))
            host.mount(self._plugin(tmp, "b", "def register(ctx):\n    ctx.provide('b', 2)"))
            host.activate("a")
            host.activate("b")
            host.deactivate("a")
            self.assertIsNone(host.service("a", None))
            self.assertEqual(host.service("b"), 2)


    def test_reactivation_clears_stale_error(self):
        """缺陷修复：FAILED 插件修好后重新激活，不得留着上一次的失败原因。

        旧行为：activate() 成功路径不重置 record.error，于是 /status 与 /plugins
        会长期显示一个早已不存在的错误。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            flag = tmp / "fail-now"
            flag.write_text("x", encoding="utf-8")
            plugin = self._plugin(tmp, "flaky", f"""
from pathlib import Path

def register(ctx):
    if Path(r"{flag}").exists():
        raise RuntimeError("首次故意失败")
    ctx.provide("svc", 1)
""")
            host = Harness()
            record = host.mount(plugin)
            host.activate(record.name)
            self.assertEqual(record.state, FAILED)
            self.assertIn("首次故意失败", record.error)

            flag.unlink()
            host.activate(record.name)
            self.assertEqual(record.state, ACTIVE)
            self.assertEqual(record.error, "", "重新激活成功后必须清空陈旧错误")
            self.assertEqual(record.provided["services"], ["svc"])


class PluginGraphTests(KernelTests):

    def test_shutdown_disposes_active_plugins_lifo_and_is_idempotent(self):
        """审计 H-06：退出回收——shutdown 停用全部 ACTIVE 插件（disposer 执行）且幂等。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "disposed.log"

            host = Harness()
            # p2 依赖 p1：激活序 p1→p2，shutdown 应先卸 p2（依赖者先卸）
            p1 = self._plugin(tmp, "p1", f"""
def register(ctx):
    ctx.effect(lambda: (lambda: open(r"{marker}", "a", encoding="utf-8").write("p1 ")))
""", inject=[])
            p2 = self._plugin(tmp, "p2", f"""
def register(ctx):
    ctx.effect(lambda: (lambda: open(r"{marker}", "a", encoding="utf-8").write("p2 ")))
""", inject=["p1"])
            host.mount(p1, name="p1")
            host.mount(p2, name="p2")
            host.activate_all()
            self.assertEqual([r.state for r in host.plugins.values()],
                             [ACTIVE, ACTIVE])

            closed = host.shutdown()
            self.assertEqual(sorted(closed), ["p1", "p2"])
            self.assertTrue(all(r.state == DISPOSED
                                for r in host.plugins.values()))
            self.assertEqual(marker.read_text(encoding="utf-8"), "p2 p1 ")
            # 幂等：二次 shutdown 无事发生、不抛错
            self.assertEqual(host.shutdown(), [])
    """批次D：依赖声明 / 拓扑激活 / 冲突可见 / 单插件热重载。"""

    def test_inject_orders_activation(self):
        """声明 inject 后，激活顺序不再由目录名字母序决定。"""
        with tempfile.TemporaryDirectory() as tmp:
            # 目录名故意让消费者排在前：a_consumer 先于 z_provider 被装载
            self._plugin(tmp, "a_consumer", """
def register(ctx):
    svc = ctx.host.service("thing")
    if svc is None:
        raise RuntimeError("依赖未就绪：provider 还没激活")
    ctx.provide("consumer.saw", svc)
""", manifest_name="consumer", inject=["provider"])
            plugin = self._plugin(tmp, "z_provider", """
def register(ctx):
    ctx.provide("thing", 42)
""", manifest_name="provider")

            host = Harness()
            host.mount_all(tmp)
            order = host.activation_order()
            self.assertLess(order.index("provider"), order.index("consumer"),
                            "被依赖者必须先激活")
            host.activate_all()
            states = {p["name"]: p["state"] for p in host.list()}
            self.assertEqual(states["consumer"], ACTIVE)
            self.assertEqual(host.service("consumer.saw"), 42)
            self.assertEqual(host.plugins["consumer"].deps, ["provider"])
            self.assertIsNotNone(plugin)

    def test_missing_dependency_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._plugin(tmp, "solo", 'def register(ctx):\n    ctx.provide("x", 1)\n',
                         inject=["nobody"])
            host = Harness()
            record = host.mount_all(tmp)[0]
            host.activate_all()
            self.assertEqual(record.state, ACTIVE, "依赖缺失不该让插件直接失败")
            self.assertTrue(any("missing-dep" in note for note in record.skipped),
                            f"应报告缺失依赖，实际: {record.skipped}")

    def test_dependency_cycle_does_not_deadlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._plugin(tmp, "cyc_a", 'def register(ctx):\n    ctx.provide("a", 1)\n',
                         inject=["cyc_b"])
            self._plugin(tmp, "cyc_b", 'def register(ctx):\n    ctx.provide("b", 2)\n',
                         inject=["cyc_a"])
            host = Harness()
            host.mount_all(tmp)
            host.activate_all()
            states = {p["name"]: p["state"] for p in host.list()}
            self.assertEqual(states["cyc_a"], ACTIVE)
            self.assertEqual(states["cyc_b"], ACTIVE)

    def test_bad_manifest_note_survives_activation(self):
        """装载期记下的问题不能被 activate() 覆写掉（此前 skipped 会被整体替换）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "badman"
            path.mkdir()
            (path / "plugin.json").write_text("{ 这不是 json", encoding="utf-8")
            (path / "register.py").write_text(
                'def register(ctx):\n    ctx.provide("x", 1)\n', encoding="utf-8")
            host = Harness()
            record = host.mount(path)
            host.activate(record.name)
            self.assertEqual(record.state, ACTIVE)
            self.assertTrue(any("bad-manifest" in note for note in record.skipped),
                            f"清单损坏的提示不该消失，实际: {record.skipped}")

    def test_service_conflict_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self._plugin(tmp, "s1", 'def register(ctx):\n    ctx.provide("dup", "one")\n')
            second = self._plugin(tmp, "s2", 'def register(ctx):\n    ctx.provide("dup", "two")\n')
            host = Harness()
            host.mount(first)
            host.mount(second)
            host.activate_all()
            self.assertEqual(host.service("dup"), "two", "后挂载者覆盖")
            self.assertEqual(host.service_conflicts.get("dup"), ["s1", "s2"],
                             "同名服务的多个提供者必须可查，而不是静默覆盖")

    def test_reload_picks_up_new_code(self):
        """热重载必须拿到磁盘上的新代码（模块缓存需被清掉）。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin(tmp, "demo",
                                  'def register(ctx):\n    ctx.provide("v", 1)\n')
            host = Harness()
            record = host.mount(plugin)
            host.activate(record.name)
            self.assertEqual(host.services["v"], 1)

            (plugin / "register.py").write_text(
                'def register(ctx):\n    ctx.provide("v", 2)\n', encoding="utf-8")
            again = host.reload("demo")
            self.assertEqual(again.state, ACTIVE)
            self.assertEqual(host.services["v"], 2, "重载后应是磁盘上的新版本")

    def test_reload_unknown_plugin_raises(self):
        host = Harness()
        with self.assertRaises(KeyError):
            host.reload("nothing")


class BuiltinPluginsTests(unittest.TestCase):
    def test_builtin_plugins_all_activate(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            states = {p["name"]: p["state"] for p in host.list()}
            for name in ("chat_loop", "models", "tools_fs", "tools_shell", "tools_search",
                         "tools_todo", "skills", "repl"):
                self.assertEqual(states.get(name), ACTIVE, name)
            self.assertIn("ask", host.services)
            self.assertIn("ui", host.services)

    def test_tools_aggregated_from_plugins(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            names = {t.name for t in host.collect_tools()}
            self.assertIn("read_file", names)
            self.assertIn("write_file", names)
            self.assertIn("run_command", names)
            self.assertIn("switch_model", names)
            for extra in ("edit_file", "search_text", "todo_write", "todo_read"):
                self.assertIn(extra, names)

    def test_model_switch_persists_and_updates_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp), models=[
                {"name": "m1", "provider": "openai", "model": "x", "api_key": "k"},
                {"name": "m2", "provider": "openai", "model": "y", "api_key": "k"},
            ])
            with patch_model_build():
                from harness.cli import build_runtime

                host = build_runtime(profile, str(tmp))
                runtime = host.service("models_runtime")
                result = runtime["set"]("m2")
                self.assertIn("m2", result)
                self.assertEqual(runtime["current"], "m2")
                # 持久化到配置
                self.assertEqual(profile.load_config()["default_model"], "m2")

    def test_shell_permission_gate(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            shell_tool = next(t for t in host.collect_tools() if t.name == "run_command")
            # 默认 ask 且无确认回调 → 拒绝
            self.assertIn("拒绝", shell_tool.invoke({"command": "echo hi"}))
            # allow → 执行
            profile = host.profile
            profile.update_config(permissions={"shell": "allow"})
            result = shell_tool.invoke({"command": "echo hi"})
            self.assertIn("hi", result)
            # confirm 回调拒绝 → 拒绝
            profile.update_config(permissions={"shell": "ask"})
            host.confirm = lambda prompt: False
            self.assertIn("拒绝", shell_tool.invoke({"command": "echo hi"}))

    def test_workspace_jail_in_tools_fs(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = allow_fs(build_host(Path(tmp)))
            write = next(t for t in host.collect_tools() if t.name == "write_file")
            read = next(t for t in host.collect_tools() if t.name == "read_file")
            write.invoke({"path": "你好.saho", "content": "打印(1)"})
            self.assertIn("打印(1)", read.invoke({"path": "你好.saho"}))

            with self.assertRaises(Exception):
                read.invoke({"path": "../escape.txt"})


class ToolTests(unittest.TestCase):
    """工具集补强：edit_file / read 分页 / list 上限 / search_text / todo / shell cwd·env。"""

    def test_edit_file_replaces_exactly(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = allow_fs(build_host(Path(tmp)))
            tool(host, "write_file").invoke({"path": "a.py", "content": "x = 1\ny = 2\n"})
            out = tool(host, "edit_file").invoke(
                {"path": "a.py", "old": "y = 2", "new": "y = 3"})
            self.assertIn("替换 1 处", out)
            self.assertIn("y = 3", tool(host, "read_file").invoke({"path": "a.py"}))
            self.assertNotIn("y = 2", tool(host, "read_file").invoke({"path": "a.py"}))

    def test_edit_file_refuses_ambiguous_or_missing(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = allow_fs(build_host(Path(tmp)))
            edit = tool(host, "edit_file")
            tool(host, "write_file").invoke({"path": "b.txt", "content": "a\na\n"})

            out = edit.invoke({"path": "b.txt", "old": "a", "new": "z"})
            self.assertIn("匹配到 2 处", out)
            self.assertEqual(tool(host, "read_file").invoke({"path": "b.txt"}).count("a"), 2,
                             "命中数不符时不得改动文件")

            self.assertIn("未找到", edit.invoke({"path": "b.txt", "old": "没有的", "new": "z"}))
            # 显式声明期望处数即可批量替换
            self.assertIn("替换 2 处", edit.invoke(
                {"path": "b.txt", "old": "a", "new": "z", "count": 2}))

    def test_edit_file_respects_permission_gate(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))  # fs=ask 且无 confirm → fail-closed
            allow_fs(host)
            tool(host, "write_file").invoke({"path": "c.txt", "content": "hello"})
            host.profile.update_config(permissions={"fs": "ask"})
            out = tool(host, "edit_file").invoke({"path": "c.txt", "old": "hello", "new": "bye"})
            self.assertIn("未获人工确认", out)
            self.assertIn("hello", tool(host, "read_file").invoke({"path": "c.txt"}))

    def test_read_file_pagination(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = allow_fs(build_host(Path(tmp)))
            content = "\n".join(f"line{i}" for i in range(1, 301))
            tool(host, "write_file").invoke({"path": "big.txt", "content": content})
            read = tool(host, "read_file")

            out = read.invoke({"path": "big.txt", "offset": 101, "limit": 50})
            self.assertIn("101: line101", out)
            self.assertIn("150: line150", out)
            self.assertNotIn("151: line151", out)
            self.assertIn("offset=151", out, "应给出续读提示")
            self.assertIn("超出文件末尾", read.invoke({"path": "big.txt", "offset": 9999}))

    def test_list_files_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = allow_fs(build_host(Path(tmp)))
            write = tool(host, "write_file")
            for i in range(6):
                write.invoke({"path": f"f{i}.txt", "content": "x"})
            out = tool(host, "list_files").invoke({"pattern": "*.txt", "max_results": 3})
            self.assertEqual(len([ln for ln in out.splitlines() if ln.endswith(".txt")]), 3)
            self.assertIn("已达上限 3 条", out)
            # 非法模式必须回一条可读错误，而不是把异常抛给模型
            self.assertIn("错误：", tool(host, "list_files").invoke({"pattern": "../*"}))

    def test_search_text(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = allow_fs(build_host(Path(tmp)))
            write = tool(host, "write_file")
            write.invoke({"path": "pkg/a.py", "content": "def foo():\n    return 1\n"})
            write.invoke({"path": "note.md", "content": "foo 出现在文档里\n"})
            search = tool(host, "search_text")

            out = search.invoke({"pattern": "foo"})
            self.assertIn("pkg/a.py:1", out)
            self.assertIn("note.md:1", out)

            scoped = search.invoke({"pattern": "foo", "glob": "**/*.py"})
            self.assertIn("pkg/a.py:1", scoped)
            self.assertNotIn("note.md", scoped)

            self.assertIn("无匹配", search.invoke({"pattern": "绝不存在的串"}))
            self.assertIn("正则表达式无效", search.invoke({"pattern": "([", "regex": True}))
            self.assertIn("错误：", search.invoke({"pattern": "foo", "glob": "../*"}))

    def test_todo_tools(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            write_todo, read_todo = tool(host, "todo_write"), tool(host, "todo_read")

            out = write_todo.invoke({"items": [
                {"content": "第一步", "status": "pending"},
                {"content": "第二步", "status": "进行中"},  # 中文别名
            ]})
            self.assertIn("[ ] 1. 第一步", out)
            self.assertIn("[~] 2. 第二步", out)
            self.assertIn("0/2 已完成", out)

            done = write_todo.invoke({"items": [
                {"content": "第一步", "status": "done"},
                {"content": "第二步", "status": "completed"},
            ]})
            self.assertIn("2/2 已完成", done)
            self.assertIn("待办清单（", read_todo.invoke({}))

            self.assertIn("status 非法", write_todo.invoke(
                {"items": [{"content": "x", "status": "胡说"}]}))
            self.assertIn("缺少 content", write_todo.invoke({"items": [{"status": "done"}]}))
            self.assertIn("需要是数组", write_todo.invoke({"items": {"content": "x"}}))

    def test_shell_cwd_and_env(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = allow_shell(build_host(Path(tmp)))
            sub = Path(tmp) / "sub"
            sub.mkdir()
            (sub / "probe.py").write_text(
                "import os\n"
                "print('CWD', os.path.basename(os.getcwd()))\n"
                "print('ENV', os.environ.get('HARNESS_PROBE', ''))\n",
                encoding="utf-8")
            run = tool(host, "run_command")

            out = run.invoke({"command": f'"{sys.executable}" probe.py', "cwd": "sub",
                              "env": {"HARNESS_PROBE": "ok"}})
            self.assertIn("exit 0", out)
            self.assertIn("CWD sub", out)
            self.assertIn("ENV ok", out)

            self.assertIn("越出工作区", run.invoke({"command": "echo x", "cwd": "../"}))
            self.assertIn("不是目录", run.invoke({"command": "echo x", "cwd": "nope"}))


class EndToEndTests(unittest.TestCase):
    def test_oneshot_chat_with_fake_model(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            ask = host.service("ask")
            result = ask("你好", session_id="t1")
            self.assertIn("假回答", result["reply"])
            # 会话持久化
            self.assertEqual(len(host.profile.load_session("t1")), 2)

    def test_tool_call_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = allow_fs(build_host(Path(tmp)))
            ask = host.service("ask")
            agent = host.service("agent_factory")()
            agent.llm.script = [
                LLMResponse(content="", tool_calls=[__import__(
                    "nanoagent.llm", fromlist=["ToolCall"]).ToolCall(
                    id="t1", name="write_file",
                    arguments={"path": "demo.txt", "content": "hello"})]),
                LLMResponse(content="写好了"),
            ]
            result = ask("写个文件", session_id="t2")
            self.assertEqual(result["tool_calls"][0]["result"], "已写入 demo.txt（1 行）")
            self.assertEqual(result["reply"], "写好了")
            self.assertTrue((Path(tmp) / "demo.txt").is_file())


class SessionTests(unittest.TestCase):

    def test_session_path_traversal_rejected(self):
        """审计 H-04：save/load_session 不得用 ..\\.. 逃出 profile 的 sessions 目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = make_profile(root)
            outside = root / "victim.json"
            outside.write_text('[{"role": "assistant", "content": "外泄"}]', encoding="utf-8")

            # 各种穿越/绝对路径/空名一律拒绝（返回 None / []）
            self.assertIsNone(profile.save_session(r"..\..\victim", [{"role": "u"}]))
            self.assertIsNone(profile.save_session("../victim", [{"role": "u"}]))
            self.assertIsNone(profile.save_session(str(outside), [{"role": "u"}]))
            self.assertIsNone(profile.save_session("", [{"role": "u"}]))
            self.assertIsNone(profile.save_session("a/b", [{"role": "u"}]))
            self.assertEqual(profile.load_session(r"..\..\victim"), [])
            self.assertEqual(profile.load_session("../victim"), [])
            self.assertFalse(outside.read_text(encoding="utf-8").startswith('[{"role": "u"}'))

            # 正常 id 不受影响
            self.assertIsNotNone(profile.save_session("s-20260926-120000", [{"role": "u"}]))
            self.assertEqual(profile.load_session("s-20260926-120000"), [{"role": "u"}])
            self.assertIn("s-20260926-120000", profile.session_ids())
    """缺陷修复：/new 必须真正开新会话（换 id + 清 agent），旧会话仍保留在磁盘。"""

    def test_new_session_does_not_replay_old_history(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            ask = host.service("ask")
            new_session = host.service("new_session")

            ask("第一句", session_id="default")
            self.assertEqual(len(host.profile.load_session("default")), 2)

            sid = new_session()
            self.assertNotEqual(sid, "default", "new_session 必须换一个会话 id")

            ask("第二句", session_id=sid)
            agent = host.service("agent_factory")()
            contents = [m.get("content") for m in agent.memory.history(sid)]
            self.assertNotIn("第一句", contents, "新会话里不得回放旧会话的历史")
            self.assertIn("第二句", contents)
            # 旧会话不是被销毁，只是不再续用
            self.assertEqual(len(host.profile.load_session("default")), 2)

    def test_ask_defaults_to_current_session(self):
        """ask 省略 session_id 时用当前会话 —— 调用方忘记传新 id 也不会串回旧会话。"""
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            ask = host.service("ask")
            sid = host.service("new_session")()

            ask("第一句")
            ask("第二句")
            self.assertEqual(len(host.profile.load_session(sid)), 4)
            self.assertEqual(host.profile.load_session("default"), [])

    def test_switching_session_rebuilds_agent(self):
        """切到另一个会话必须重建 agent，否则记忆里仍是上一个会话的内容。"""
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            ask = host.service("ask")

            ask("会话甲的提问", session_id="a")
            ask("会话乙的提问", session_id="b")
            agent = host.service("agent_factory")()
            contents = [m.get("content") for m in agent.memory.history("b")]
            self.assertIn("会话乙的提问", contents)
            self.assertNotIn("会话甲的提问", contents, "会话之间不得串味")

    def test_new_session_ids_do_not_collide_within_one_second(self):
        """同一秒内连续新建会话时，id 必须仍然唯一（否则会覆盖彼此的历史）。"""
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            ask = host.service("ask")
            new_session = host.service("new_session")

            first = new_session()
            ask("落盘", first)  # 使其出现在 session_ids()
            second = new_session()
            self.assertNotEqual(first, second)


class CliTests(unittest.TestCase):
    """批次C：CLI 与桌面端能力对齐（模型增删 / 会话列表 / 插件脚手架 / 坏条目容错）。"""

    def test_model_add_use_remove_roundtrip(self):
        import contextlib
        import io

        from harness.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            base = ["--home", tmp]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main([*base, "model", "add", "mine", "--model", "gpt-x",
                           "--api-key", "sk-1", "--default"])
            self.assertEqual(rc, 0)
            self.assertIn("已添加模型 mine", buf.getvalue())

            # 重名必须拒绝，不能悄悄覆盖
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([*base, "model", "add", "mine", "--model", "y"]), 1)

            config = json.loads((Path(tmp) / "profiles" / "default" / "config.json")
                                .read_text(encoding="utf-8"))
            names = [m["name"] for m in config["models"]]
            self.assertIn("mine", names)
            self.assertEqual(config["default_model"], "mine")

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([*base, "model", "remove", "mine"]), 0)
            config = json.loads((Path(tmp) / "profiles" / "default" / "config.json")
                                .read_text(encoding="utf-8"))
            names = [m["name"] for m in config["models"]]
            self.assertNotIn("mine", names)
            # 关键不变量：default_model 不能指向已不存在的模型
            self.assertIn(config["default_model"], [*names, ""],
                          "删掉默认模型后不能留悬空引用")

            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([*base, "model", "remove", "mine"]), 1)

    def test_model_use_rejects_bad_name_even_when_pool_empty(self):
        """审计 H-08b：models 全不可用（order 为空）时，坏名也不能持久化为默认模型。"""
        import contextlib
        import io

        from harness.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            base = ["--home", tmp]
            # 占位 key 让 models 插件构建失败 → order 为空
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main([*base, "model", "add", "demo", "--model", "gpt-x",
                           "--api-key", "sk-..."])
            self.assertEqual(rc, 0)

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = main([*base, "model", "use", "bogus"])
            self.assertEqual(rc, 1)
            self.assertIn("模型不存在", err.getvalue())

            config = json.loads((Path(tmp) / "profiles" / "default" / "config.json")
                                .read_text(encoding="utf-8"))
            self.assertNotEqual(config["default_model"], "bogus",
                                "坏模型名不能被持久化成默认模型")

    def test_bad_model_entry_only_skips_itself(self):
        """一个模型写错（如 ${ENV} 未设置）不能连累其它模型全部消失。"""
        self.assertNotIn("HARNESS_UNSET_KEY_XYZ", os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp), models=[
                {"name": "ok", "provider": "openai", "model": "x", "api_key": "k"},
                {"name": "bad", "provider": "openai", "model": "y",
                 "api_key": "${HARNESS_UNSET_KEY_XYZ}"},
            ], default="ok")
            with patch_model_build():
                from harness.cli import build_runtime

                host = build_runtime(profile, str(tmp))
                runtime = host.service("models_runtime")
                self.assertEqual(runtime["order"], ["ok"])
                self.assertIn("bad", runtime["errors"])
                self.assertEqual(runtime["current"], "ok")
                states = {p["name"]: p["state"] for p in host.list()}
                self.assertEqual(states["models"], ACTIVE, "models 插件本身必须仍然激活")

    def test_plugin_new_scaffold_then_install(self):
        import contextlib
        import io

        from harness.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            base = ["--home", tmp]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([*base, "plugin", "new", "demo", "--dir", tmp]), 0)
            target = Path(tmp) / "demo"
            self.assertTrue((target / "plugin.json").is_file())
            source = (target / "register.py").read_text(encoding="utf-8")
            self.assertIn("def register(ctx)", source)
            self.assertIn("demo 插件", source, "模板里的占位名应被替换")
            self.assertNotIn("__NAME__", source)

            # 重名与非法名都要拒绝
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([*base, "plugin", "new", "demo", "--dir", tmp]), 1)
                self.assertEqual(main([*base, "plugin", "new", "a/b", "--dir", tmp]), 1)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(main([*base, "plugin", "add", str(target)]), 0)
            self.assertIn("已激活: demo", buf.getvalue(), "装完应当场校验并激活")

    def test_sessions_command_lists_history(self):
        import contextlib
        import io

        from harness.cli import main

        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            host.service("ask")("问题一", session_id="sess-a")
            host.service("ask")("问题二", session_id="sess-b")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(["--home", str(Path(tmp) / ".harness"), "--profile", "test",
                           "sessions"])
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("sess-a", text)
            self.assertIn("sess-b", text)
            self.assertIn("2 条消息", text)


class ReplSessionTests(unittest.TestCase):
    """启动时默认续用最近一次会话 —— 这也是与桌面端对齐的一部分。"""

    def test_latest_session_picks_most_recent(self):
        import time as _time

        from harness.builtins.repl.register import _latest_session

        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            ask = host.service("ask")
            ask("旧会话", session_id="old")
            _time.sleep(1.1)  # 文件 mtime 精度按秒
            ask("新会话", session_id="new")
            self.assertEqual(_latest_session(host), "new")

    def test_latest_session_falls_back_when_empty(self):
        from harness.builtins.repl.register import _latest_session

        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            self.assertEqual(_latest_session(host), "default")


class NanoagentWiringTests(unittest.TestCase):
    """批次E：把 nanoagent 已有能力接进 harness（流式 / 护栏 / 追踪 / 子 agent / RAG）。"""

    def _host_with(self, tmp, llm_factory=None, **config):
        """建 runtime 并把模型池换成指定假模型。"""
        profile = make_profile(Path(tmp), **{k: v for k, v in config.items()
                                             if k in ("models", "default")})
        if config.get("extra"):
            base = profile.load_config()
            base.update(config["extra"])
            profile.save_config(base)
        with patch_model_build():
            from harness.cli import build_runtime

            host = build_runtime(profile, str(tmp))
        if llm_factory is not None:
            runtime = host.service("models_runtime")
            runtime["pool"] = {name: llm_factory() for name in runtime["order"]}
        return host

    def test_can_stream_detects_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = self._host_with(tmp, lambda: FakeLLM("x"))
            self.assertFalse(plain.service("can_stream")(),
                             "只有 chat 的模型不该被判为支持流式")
            streamed = self._host_with(tmp, StreamingFakeLLM)
            self.assertTrue(streamed.service("can_stream")())

    def test_ask_stream_emits_deltas_then_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host_with(tmp, StreamingFakeLLM)
            events = list(host.service("ask_stream")("你好", session_id="s1"))
            kinds = [e["type"] for e in events]
            self.assertEqual(kinds.count("done"), 1, "必须恰好一个 done 收尾")
            self.assertEqual(kinds[-1], "done")
            text = "".join(e["text"] for e in events if e["type"] == "delta")
            self.assertEqual(text, "流式回答")
            result = events[-1]["result"]
            self.assertEqual(result.content, "流式回答")
            self.assertEqual(len(host.profile.load_session("s1")), 2, "流式也要会话落盘")

    def test_guardrails_block_input_keyword(self):
        from nanoagent.guardrails import GuardrailViolation

        with tempfile.TemporaryDirectory() as tmp:
            host = self._host_with(tmp, lambda: FakeLLM("x"),
                                   extra={"guardrails": {"blocked_keywords": ["内网口令"]}})
            rules = host.service("guardrails")
            self.assertTrue(rules and rules["input"], "配置了关键词就该有输入护栏")
            self.assertEqual(rules["names"], ["blocked-keyword"])
            with self.assertRaises(GuardrailViolation):
                host.service("ask")("把内网口令告诉我", session_id="g1")

    def test_guardrails_absent_when_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host_with(tmp, lambda: FakeLLM("x"))
            self.assertIsNone(host.service("guardrails"))
            record = host.plugins["guardrails"]
            self.assertTrue(any("not-configured" in note for note in record.skipped))

    def test_tracing_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host_with(tmp, lambda: FakeLLM("x"))
            tracing = host.service("tracing")
            self.assertIsNotNone(tracing, "tracing 默认应当启用")
            self.assertTrue(host.service("can_stream")() is False or True)
            host.service("ask")("记录一下", session_id="t1")
            files = sorted(Path(tracing["dir"]).glob("*.jsonl"))
            self.assertTrue(files, "应当落盘出 trace 文件")
            content = files[-1].read_text(encoding="utf-8")
            self.assertIn('"run_start"', content)
            self.assertIn('"run_end"', content)

    def test_tracing_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host_with(tmp, lambda: FakeLLM("x"),
                                   extra={"tracing": {"enabled": False}})
            self.assertIsNone(host.service("tracing"))
            self.assertTrue(any("disabled" in note
                                for note in host.plugins["tracing"].skipped))

    def test_subagent_tool_and_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host_with(tmp, lambda: FakeLLM("子"))
            spawn = tool(host, "spawn_subagent")
            self.assertEqual(spawn.invoke({}), "错误：task 不能为空")
            out = spawn.invoke({"task": "查一件事"})
            self.assertIn("假回答", out, "子 agent 应复用主 agent 的模型跑出结果")
            # 子 agent 的工具集必须剪掉递归入口
            sub = host.service("subagent_factory")()
            names = set(sub.tools.names())
            self.assertNotIn("spawn_subagent", names)
            self.assertNotIn("switch_model", names)
            self.assertIn("read_file", names)

    def test_knowledge_tools_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host_with(tmp, lambda: FakeLLM("k"))
            names = {t.name for t in host.collect_tools()}
            self.assertIn("search_knowledge", names)
            self.assertIn("index_knowledge", names)
            self.assertIn("知识库还是空的", tool(host, "search_knowledge").invoke({"query": "啥"}))

    @unittest.skipUnless(shutil.which("git"), "需要 git 才能测地址安装")
    def test_install_plugin_from_git_url(self):
        """支持 git 地址安装（此前只吃本地目录）。

        用本地 file:// 仓库做真实浅克隆：同时验证「仓库根不是插件、取唯一子目录」
        与「不带 .git 复制进 profile」。
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            plugin_dir = repo / "myplugin"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "plugin.json").write_text('{"name": "remote"}', encoding="utf-8")
            (plugin_dir / "register.py").write_text(
                'def register(ctx):\n    ctx.provide("remote.svc", 1)\n', encoding="utf-8")

            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, check=True,
                                      capture_output=True, timeout=60)

            git("init", "-q")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "t")
            git("add", "-A")
            git("commit", "-q", "-m", "init")

            profile = make_profile(Path(tmp) / "profile-home")
            dest = profile.install_plugin(f"file://{repo.as_posix()}")
            self.assertTrue((dest / "register.py").is_file())
            self.assertFalse((dest / ".git").exists(), "不该把 .git 复制进 profile")
            self.assertTrue(profile.remove_plugin(dest.name))


class ShellApprovalTests(unittest.TestCase):
    """批次F：shell 权限门的三级判定（白名单 → 会话记忆 → 询问）与审批审计。"""

    def _host(self, tmp, **permissions):
        host = build_host(Path(tmp))
        base = {"shell": "ask", "fs": "ask"}
        base.update(permissions)
        host.profile.update_config(permissions=base)
        return host

    def _shell(self, host):
        return next(t for t in host.collect_tools() if t.name == "run_command")

    def _audit_records(self, host):
        path = Path(host.profile.root) / "audit" / "shell.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_allowlist_skips_confirmation(self):
        """白名单命中即放行 —— 即使确认回调明确说不。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp, shell_allow=["echo hi"])
            host.confirm = lambda prompt: False
            self.assertIn("hi", self._shell(host).invoke({"command": "echo hi"}))

    def test_allowlist_glob_matches_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp, shell_allow=["echo*"])
            host.confirm = lambda prompt: False
            self.assertIn("world", self._shell(host).invoke({"command": "echo world"}))

    def test_allowlist_whitespace_folded(self):
        """`echo    hi` 与白名单里的 `echo hi` 视为同一条命令。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp, shell_allow=["echo hi"])
            host.confirm = lambda prompt: False
            self.assertIn("hi", self._shell(host).invoke({"command": "echo   hi"}))

    def test_non_matching_command_still_asks(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp, shell_allow=["git status"])
            host.confirm = lambda prompt: False
            self.assertIn("拒绝", self._shell(host).invoke({"command": "echo hi"}))

    def test_session_remembers_user_approval(self):
        """批准一次之后，同一条命令本会话内不再询问。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            calls = []

            def confirm(prompt):
                calls.append(prompt)
                return True

            host.confirm = confirm
            tool = self._shell(host)
            self.assertIn("hi", tool.invoke({"command": "echo hi"}))
            self.assertEqual(len(calls), 1)
            # 第二次：确认回调换成「一律拒绝」，但因为已记住，仍然放行且不再询问
            host.confirm = lambda prompt: False
            self.assertIn("hi", tool.invoke({"command": "echo hi"}))
            self.assertEqual(len(calls), 1, "已批准的命令不该重复询问")

    def test_session_memory_is_exact_not_prefix(self):
        """`echo hi` 获批 ≠ `echo hi && rm -rf x` 获批：会话记忆不做前缀推断。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            host.confirm = lambda prompt: True
            tool = self._shell(host)
            tool.invoke({"command": "echo hi"})
            host.confirm = lambda prompt: False
            self.assertIn("拒绝", tool.invoke({"command": "echo hi && echo bye"}))

    def test_deny_overrides_allowlist_and_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp, shell="deny", shell_allow=["echo*"])
            host.confirm = lambda prompt: True
            self.assertIn("拒绝", self._shell(host).invoke({"command": "echo hi"}))

    def test_audit_log_records_each_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp, shell_allow=["echo*"])
            host.confirm = lambda prompt: False
            tool = self._shell(host)
            tool.invoke({"command": "echo hi"})        # 白名单放行
            tool.invoke({"command": "rm -rf /nonexistent"})  # 用户拒绝
            records = self._audit_records(host)
            sources = {r["source"] for r in records}
            self.assertIn("allowlist:echo*", sources)
            self.assertIn("user-denied", sources)
            decisions = {r["decision"] for r in records}
            self.assertEqual(decisions, {"allow", "deny"})
            self.assertTrue(all("ts" in r and "command" in r for r in records))

    def test_audit_records_no_confirmer_when_channel_absent(self):
        """没有确认通道（非交互环境）时也必须留档：拒绝的理由是「没人能确认」。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            host.confirm = None
            self.assertIn("拒绝", self._shell(host).invoke({"command": "echo hi"}))
            self.assertEqual([r["source"] for r in self._audit_records(host)], ["no-confirmer"])

    def test_audit_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp, shell_allow=["echo*"], audit=False)
            host.confirm = lambda prompt: False
            self._shell(host).invoke({"command": "echo hi"})
            self.assertEqual(self._audit_records(host), [])

    def test_clear_session_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            host.confirm = lambda prompt: True
            self._shell(host).invoke({"command": "echo hi"})
            service = host.service("shell_permissions")
            self.assertEqual(service["approved"](), ["echo hi"])
            self.assertEqual(service["clear"](), 1)
            self.assertEqual(service["approved"](), [])


class AllowlistConfigTests(unittest.TestCase):
    """批次F：白名单的归一化与匹配（配置层，不依赖 harness 运行时）。"""

    def test_normalize_drops_junk_and_dedupes(self):
        from harness.config import normalize_allowlist
        self.assertEqual(normalize_allowlist(None), [])
        self.assertEqual(normalize_allowlist("echo hi"), [], "非列表整段忽略（fail-closed）")
        self.assertEqual(normalize_allowlist(["git status", "  ", 3, None, "git   status"]),
                         ["git status"], "空白条目与非字符串丢弃，空白折叠后去重")

    def test_shell_allowlist_missing_is_empty(self):
        from harness.config import shell_allowlist
        self.assertEqual(shell_allowlist({}), [])
        self.assertEqual(shell_allowlist({"permissions": "oops"}), [])

    def test_matches_allowlist(self):
        from harness.config import matches_allowlist
        patterns = ["git status", "git log*"]
        self.assertEqual(matches_allowlist("git status", patterns), "git status")
        self.assertEqual(matches_allowlist("git  status", patterns), "git status")
        self.assertEqual(matches_allowlist("git log --oneline", patterns), "git log*")
        self.assertIsNone(matches_allowlist("git push", patterns))
        self.assertIsNone(matches_allowlist("", patterns))

    def test_audit_enabled_defaults_true_and_validates(self):
        from harness.config import audit_enabled
        self.assertTrue(audit_enabled({}))
        self.assertFalse(audit_enabled({"permissions": {"audit": False}}))
        self.assertTrue(audit_enabled({"permissions": {"audit": "no"}}), "非法值不丢记录")

    def test_permissions_roundtrip_keeps_new_keys(self):
        """新增键必须在 save/load 往返中存活（H-07 同类问题：归一会丢键）。"""
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp))
            profile.update_config(permissions={
                "shell": "ask", "fs": "ask",
                "shell_allow": ["git status"], "audit": False,
            })
            perms = profile.load_config()["permissions"]
            self.assertEqual(perms["shell_allow"], ["git status"])
            self.assertIs(perms["audit"], False)

    def test_partial_permission_edit_uses_mutate_config(self):
        """改单个权限键必须走 mutate_config —— update_config 会替换整个 permissions 段。

        这是既有的约定（cli.py 改 models 就用 mutate_config），新增的 shell_allow /
        audit 让「顺手用 update_config 改 shell」的代价从「丢一个键」变成「丢三个键」，
        所以在这里把正确姿势钉成测试。
        """
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp))
            profile.update_config(permissions={
                "shell": "ask", "fs": "ask",
                "shell_allow": ["git status"], "audit": False,
            })

            def _enable_shell(config):
                config["permissions"]["shell"] = "allow"

            profile.mutate_config(_enable_shell)
            perms = profile.load_config()["permissions"]
            self.assertEqual(perms["shell"], "allow")
            self.assertEqual(perms["shell_allow"], ["git status"], "兄弟键必须还在")
            self.assertIs(perms["audit"], False, "兄弟键必须还在")


class AsyncToolChatTests(unittest.TestCase):
    """审计 H-02：协程工具 + 同步 LLM 的对话链路。

    此前 ask() 检测到异步工具就切 arun，而 models 只构建同步 LLM，
    配了 MCP（工具 is_async）对话必抛 TypeError。现在统一走同步 run，
    协程工具由 nanoagent 的同步执行路径单独起循环跑完。
    """

    def test_ask_with_coroutine_tool_and_sync_llm(self):
        import asyncio

        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            agent = host.service("agent_factory")()

            async def echo(word: str) -> str:
                """回显单词。"""
                return f"echo:{word}"

            agent.tools.register(echo)
            self.assertTrue(agent.tools.get("echo").is_async)

            llm = agent.llm
            self.assertFalse(hasattr(llm, "achat"))  # 同步 LLM
            from nanoagent.llm import ToolCall

            llm.script = [LLMResponse(
                content="", tool_calls=[ToolCall(
                    id="t1", name="echo", arguments={"word": "hi"})])]
            llm.script.append(LLMResponse(content="工具已调用"))

            ask = host.service("ask")
            result = ask("调用 echo")
            self.assertEqual(result["reply"], "工具已调用")
            self.assertTrue(asyncio.iscoroutinefunction(echo))

    def test_mcp_style_tools_are_wrapped_sync(self):
        """mcp_client 把异步 Tool 包装成同步闭包（同 schema），is_async 恒 False。"""
        import asyncio

        from nanoagent.tools import Tool, make_tool

        with tempfile.TemporaryDirectory() as tmp:
            host = build_host(Path(tmp))
            agent = host.service("agent_factory")()

            async def remote(query: str) -> str:
                """模拟 MCP 工具。"""
                return f"remote:{query}"

            async_tool = make_tool(remote)
            self.assertTrue(async_tool.is_async)

            # 与 mcp_client.wrap_sync 相同的包装方式
            def call(**arguments):
                return asyncio.run(async_tool.arun(arguments))

            call.__name__ = async_tool.name
            call.__doc__ = async_tool.description
            sync_tool = Tool(name=async_tool.name, description=async_tool.description,
                             parameters=async_tool.parameters, func=call, thread_safe=False)
            agent.tools.register(sync_tool)

            self.assertFalse(agent.tools.get("remote").is_async)
            result = agent.tools.get("remote").invoke({"query": "x"})
            self.assertEqual(result, "remote:x")


class ModelKeyTests(unittest.TestCase):
    """第三批 #15：API Key 支持环境变量注入，避免明文落盘。"""

    def test_placeholder_key_is_treated_as_unconfigured(self):
        """审计 H-11：占位 "sk-..." 不能进可用池，否则首话报上游 401 而非「没配 key」。"""
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(
                Path(tmp),
                models=[{"name": "demo", "provider": "openai",
                         "model": "x", "api_key": "sk-..."}],
                default="demo")
            runtime = host.service("models_runtime") or {}
            self.assertEqual(runtime.get("order"), [])
            self.assertIn("占位符", (runtime.get("errors") or {}).get("demo", ""))

    def test_env_ref_forms(self):
        import os

        from harness.builtins.models.register import _resolve_api_key

        os.environ["DEMO_KEY"] = "sk-real"
        self.addCleanup(os.environ.pop, "DEMO_KEY", None)
        for raw in ["${DEMO_KEY}", "$DEMO_KEY", "env:DEMO_KEY", " ${DEMO_KEY} "]:
            self.assertEqual(_resolve_api_key(raw, "openai", "m"), "sk-real", raw)

    def test_plain_key_untouched(self):
        from harness.builtins.models.register import _resolve_api_key

        self.assertEqual(_resolve_api_key("sk-plain", "openai", "m"), "sk-plain")

    def test_provider_env_fallback(self):
        import os

        from harness.builtins.models.register import _resolve_api_key

        os.environ["ANTHROPIC_API_KEY"] = "sk-ant"
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY", None)
        self.assertEqual(_resolve_api_key("", "anthropic", "m"), "sk-ant")

    def test_missing_env_ref_raises(self):
        import os

        from harness.builtins.models.register import _resolve_api_key

        os.environ.pop("NOPE_KEY", None)
        with self.assertRaises(ValueError):
            _resolve_api_key("${NOPE_KEY}", "openai", "m")


class ProfileTests(unittest.TestCase):
    def test_plugin_install_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp))
            src = Path(tmp) / "my-plugin"
            src.mkdir()
            (src / "plugin.json").write_text('{"name": "my-plugin"}', encoding="utf-8")
            dest = profile.install_plugin(src)
            self.assertTrue(dest.is_dir())
            self.assertEqual(dest.name, "my-plugin")
            profile.remove_plugin("my-plugin")
            self.assertFalse((profile.plugins_dir / "my-plugin").exists())

    def test_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp))
            config = profile.update_config(default_model="other")
            self.assertEqual(config["default_model"], "other")
            self.assertEqual(profile.load_config()["default_model"], "other")

    def test_permission_matrix_fail_closed(self):
        """H-01：只有规范 allow/大小写变体放行，其余非法值一律收敛为 ask（绝不 fail-open）。"""
        from harness.config import permission_mode

        def mode(raw):
            return permission_mode({"permissions": {"shell": raw}}, "shell")

        # 规范 allow + 大小写/空白变体 → 放行
        for raw in ["allow", "ALLOW", "Allow", " allow "]:
            self.assertEqual(mode(raw), "allow", repr(raw))
        # 规范 deny + 变体 → 仍然是 deny（保守拒绝，不放行）
        for raw in ["deny", "DENY", "Deny", " deny "]:
            self.assertEqual(mode(raw), "deny", repr(raw))
        # 非法 / 自造值 → 收敛为 ask，绝不放行
        for raw in ["ask", "no", "", None, 0, 1, True, False,
                    "yes", "ALLOW_ALL", "*", [], {}]:
            self.assertNotEqual(mode(raw), "allow", repr(raw))
            self.assertEqual(mode(raw), "ask", repr(raw))
        # 缺省 / permissions 非字典 → 保守 ask
        self.assertEqual(permission_mode({}, "shell"), "ask")
        self.assertEqual(permission_mode({"permissions": "oops"}, "shell"), "ask")

    def test_update_config_concurrent_increments(self):
        """H-03：并发读-改-写（mutate_config 持锁）不得丢更新。"""
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp))

            def bump():
                for _ in range(40):
                    profile.mutate_config(
                        lambda c: c.__setitem__("counter", int(c.get("counter") or 0) + 1)
                    )

            threads = [threading.Thread(target=bump) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(profile.load_config()["counter"], 160)

    def test_corrupt_config_backed_up_and_raises(self):
        """H-03：配置损坏时抛 ConfigError 并保留备份，绝不静默覆盖。"""
        from harness.config import ConfigError

        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp))
            path = profile.config_path
            path.write_text("{ 这不是 json", encoding="utf-8")
            with self.assertRaises(ConfigError):
                profile.load_config()
            backups = list(path.parent.glob(path.name + ".corrupt-*"))
            self.assertTrue(backups, "损坏的配置应留下备份")
            self.assertIn("这不是 json", backups[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
