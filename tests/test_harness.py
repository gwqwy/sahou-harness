"""sahou-harness 测试：内核生命周期、插件装载、模型切换、权限门、端到端对话。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.config import Profile
from harness.kernel import ACTIVE, DISPOSED, FAILED, Harness
from nanoagent.llm import LLMResponse

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
    from harness.cli import BUILTINS_DIR, build_runtime

    profile = make_profile(tmp, **config_overrides)
    host = build_runtime(profile, str(tmp))
    return host


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
    def _plugin(self, tmp, name: str, body: str) -> Path:
        tmp = Path(tmp)
        path = tmp / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
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


class BuiltinPluginsTests(unittest.TestCase):
    def test_builtin_plugins_all_activate(self):
        with tempfile.TemporaryDirectory() as tmp, patch_model_build():
            host = build_host(Path(tmp))
            states = {p["name"]: p["state"] for p in host.list()}
            for name in ("chat_loop", "models", "tools_fs", "tools_shell", "skills", "repl"):
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

    def test_model_switch_persists_and_updates_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp), models=[
                {"name": "m1", "provider": "openai", "model": "x", "api_key": "k"},
                {"name": "m2", "provider": "openai", "model": "y", "api_key": "k"},
            ])
            with patch_model_build():
                from harness.cli import BUILTINS_DIR, build_runtime

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
            from nanoagent.tools import Tool

            with self.assertRaises(Exception):
                read.invoke({"path": "../escape.txt"})


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


class ModelKeyTests(unittest.TestCase):
    """第三批 #15：API Key 支持环境变量注入，避免明文落盘。"""

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
