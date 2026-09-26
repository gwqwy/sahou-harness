"""卅 harness 桌面端：pywebview 原生窗口，进程内 API 直连（无 HTTP、无浏览器）。

形态与视觉对齐 dsh-desktop（桌面壳 + profile 隔离 + 近黑工作台界面），
但不引入 Electron：pywebview 在 Windows 上是 WebView2 原生窗口，HTML 仅作渲染层，
页面通过 window.pywebview.api.* 直接调用本模块的 DesktopApp 方法。

用法：
    sha desktop            # 打开桌面窗口（需要 pip install pywebview）
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import PERMISSION_MODES, ConfigError, permission_mode
from .kernel import ACTIVE

THINKING_LEVELS = ("off", "low", "medium", "high")
THEME_MODES = ("dark", "light", "system")
MARKET_URL = ("https://raw.githubusercontent.com/awesome-dsh-plugin/"
              "awesome-dsh-plugin/main/README.zh.md")
# 权限确认对话框的最长等待（秒）：超时按「拒绝」处理
CONFIRM_TIMEOUT = 120.0


def _validate_url(url: str) -> str:
    """外发请求前的安全校验：仅 http/https，拒绝本地/环回/私有/保留地址。

    只对「主机本身就是 IP 字面量」做私有段判定；域名不做 DNS 结果判定 ——
    常见代理（TUN/fake-ip）会把公网域名解析成保留网段，按 DNS 拒绝会误杀全部请求。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("仅允许 http/https 链接")
    host = (parsed.hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError(f"不允许访问本地地址: {host or '(空)'}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host  # 普通域名
    if (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        raise ValueError(f"不允许访问私有/保留地址: {host}")
    return host


def _fetch_text(url: str, timeout: int = 20) -> str:
    _validate_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "sha-harness-market/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _mask_key(key: Any) -> str:
    """把 API Key 掩码后再回传前端（P2 #8）。

    短 key 一律显示 ``****``；长 key 保留首 3 位与末 4 位便于用户辨认。
    保存时前端若回传掩码或留空，则保持原密钥不变。
    """
    text = str(key or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "****"
    return f"{text[:3]}***{text[-4:]}"


def _parse_market(markdown: str) -> tuple[list[dict], list[str]]:
    """解析精选列表：`### 分类` 下的 `- [名称](链接) — 描述`。"""
    items: list[dict] = []
    categories: list[str] = []
    current = ""
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("### "):
            current = line[4:].strip()
            if current and current not in categories:
                categories.append(current)
        elif line.startswith("- [") and current:
            m = re.match(r"^- \[(.+?)\]\((\S+?)\)\s+[—–-]\s+(.*)$", line)
            if m:
                items.append({"name": m.group(1), "url": m.group(2),
                              "desc": m.group(3).strip(), "category": current})
    return items, categories


# ---------------------------------------------------------------------------
# 进程内 API（可独立测试，不依赖窗口）
# ---------------------------------------------------------------------------


class DesktopApp:
    """js_api 后端：窗口里的每次调用都落在这些方法上。"""

    def __init__(self, host):
        self.host = host
        self._window = None          # pywebview 窗口句柄，start 后才有
        self._lock = threading.RLock()  # js_api 每次调用一个线程，对话需串行
        self._current_session = "default"
        self._market_cache = None       # (时间戳, items, categories)，10 分钟 TTL
        self._market_lock = threading.Lock()
        host.confirm = self._confirm  # shell/文件写入权限 ask → 窗口内确认对话框

    # -- 内部工具 -----------------------------------------------------------
    @staticmethod
    def _ws_id(path: str) -> str:
        return "ws-" + hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8]

    def _config(self) -> dict:
        return self.host.profile.load_config()

    def _current_ws(self, config: dict | None = None) -> str:
        config = config or self._config()
        return str(config.get("current_workspace") or "")

    def _register_workspace(self, path: str, config: dict | None = None) -> dict:
        """把目录注册进工作区列表（同路径复用既有条目）。"""
        config = config or self._config()
        entries = list(config.get("workspaces") or [])
        resolved = str(Path(path).resolve())
        for entry in entries:
            if str(entry.get("path")) == resolved:
                return entry
        entry = {"id": self._ws_id(resolved), "name": Path(resolved).name, "path": resolved}
        entries.append(entry)
        self.host.profile.update_config(workspaces=entries)
        entry_list = self._config().get("workspaces") or []
        return entry_list[-1] if entry_list else entry

    def _touch_session(self, session_id: str) -> None:
        config = self._config()
        meta = dict(config.get("sessions_meta") or {})
        info = dict(meta.get(session_id) or {})
        info.setdefault("name", session_id)
        info["ws"] = info.get("ws") or self._current_ws(config)
        info["updated"] = time.time()
        meta[session_id] = info
        self.host.profile.update_config(sessions_meta=meta)

    # -- 权限确认（窗口内同步对话框；无窗口 fail-closed） ---------------------
    def _confirm(self, prompt: str) -> bool:
        if self._window is None:
            return False
        try:
            self._window.evaluate_js(
                "window.__dialogResult=null;showDialog({title:'权限确认',"
                f"message:{json.dumps(prompt, ensure_ascii=False)},"
                "confirmText:'允许',cancelText:'拒绝'})"
            )
        except Exception:  # noqa: BLE001
            return False
        # 有界等待 + 指数退避：原先固定轮询 36000 次（最长 1 小时）且每次都跨进程调用
        # evaluate_js；这里总超时 CONFIRM_TIMEOUT，未响应即按拒绝处理（fail-closed）。
        deadline = time.monotonic() + CONFIRM_TIMEOUT
        interval = 0.1
        while time.monotonic() < deadline:
            try:
                value = self._window.evaluate_js("window.__dialogResult")
            except Exception:  # noqa: BLE001 —— 窗口已关
                return False
            if value is not None:
                return bool(value)
            time.sleep(interval)
            interval = min(interval * 1.5, 1.0)
        return False

    # -- 状态 ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        runtime = self.host.service("models_runtime") or {}
        config = self._config()
        ws_entries = {e.get("id"): e for e in (config.get("workspaces") or [])}
        current_ws = self._current_ws(config)
        ws_entry = ws_entries.get(current_ws) or {}
        meta = (config.get("sessions_meta") or {}).get(self._current_session) or {}
        llm = None
        try:
            llm = getattr(self.host.service("agent_factory")(), "llm", None)
        except Exception:  # noqa: BLE001 —— 未配置模型时静默
            pass
        usage = dict(getattr(llm, "total_usage", {}) or {})
        return {
            "ok": True,
            "model": runtime.get("current", ""),
            "models": list(runtime.get("order", [])),
            "profile": str(self.host.profile.root),
            "workspace": str(getattr(self.host, "workspace", "")),
            "workspace_name": ws_entry.get("name", ""),
            "session": self._current_session,
            "session_name": meta.get("name", self._current_session),
            "shell_permission": permission_mode(config, "shell"),
            "fs_permission": permission_mode(config, "fs"),
            "thinking_level": config.get("thinking_level") or "off",
            "theme": config.get("theme") if config.get("theme") in THEME_MODES else "light",
            "context_window": int(getattr(llm, "context_window", 0) or 0),
            "usage": usage,
        }

    def set_permission(self, kind: str, mode: str) -> dict[str, Any]:
        if kind not in ("shell", "fs"):
            return {"ok": False, "error": f"未知权限类别: {kind}"}
        if mode not in PERMISSION_MODES:
            return {"ok": False, "error": f"未知权限模式: {mode}"}
        config = self._config()
        permissions = dict(config.get("permissions") or {})
        permissions[kind] = mode
        self.host.profile.update_config(permissions=permissions)
        return {"ok": True, "kind": kind, "mode": mode}

    # -- 工作区 ---------------------------------------------------------------
    def set_workspace(self, path: str) -> dict[str, Any]:
        path = str(path or "").strip()
        if not path:
            return {"ok": False, "error": "路径为空"}
        resolved = Path(path).resolve()
        if not resolved.is_dir():
            return {"ok": False, "error": f"目录不存在: {path}"}
        self.host.workspace = str(resolved)
        entry = self._register_workspace(str(resolved))
        self.host.profile.update_config(
            workspace=str(resolved), current_workspace=entry["id"]
        )
        return {"ok": True, "workspace": str(resolved), "workspace_name": entry["name"]}

    def choose_workspace(self) -> dict[str, Any]:
        """弹出原生文件夹选择对话框，选中后切换工作区。"""
        if self._window is None:
            return {"ok": False, "error": "窗口未就绪"}
        try:
            import webview

            picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:  # noqa: BLE001 —— 用户取消或对话框失败
            return {"ok": False, "error": str(exc)}
        if not picked:
            return {"ok": False, "error": ""}
        return self.set_workspace(picked[0] if isinstance(picked, (list, tuple)) else picked)

    def rename_workspace(self, name: str) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "error": "名称为空"}
        config = self._config()
        current = self._current_ws(config)
        entries = list(config.get("workspaces") or [])
        for entry in entries:
            if entry.get("id") == current:
                entry["name"] = name
                self.host.profile.update_config(workspaces=entries)
                return {"ok": True, "id": current, "name": name}
        return {"ok": False, "error": "当前目录尚未注册为工作区"}

    def workspaces(self) -> dict[str, Any]:
        config = self._config()
        current = self._current_ws(config)
        return {"ok": True,
                "workspaces": list(config.get("workspaces") or []),
                "current": current}

    # -- 思考级别 -------------------------------------------------------------
    def set_thinking(self, level: str) -> dict[str, Any]:
        if level not in THINKING_LEVELS:
            return {"ok": False, "error": f"未知思考级别: {level}"}
        self.host.profile.update_config(thinking_level=level)
        runtime = self.host.service("models_runtime") or {}
        effort = None if level == "off" else level
        for llm in (runtime.get("pool") or {}).values():
            llm.reasoning_effort = effort
        return {"ok": True, "thinking_level": level}

    # -- 主题 -----------------------------------------------------------------
    def set_theme(self, mode: str) -> dict[str, Any]:
        if mode not in THEME_MODES:
            return {"ok": False, "error": f"未知主题: {mode}"}
        self.host.profile.update_config(theme=mode)
        return {"ok": True, "theme": mode}

    def open_url(self, url: str) -> dict[str, Any]:
        """在系统默认浏览器打开链接（市场条目 → 插件仓库）。"""
        import webbrowser

        try:
            _validate_url(url)
            webbrowser.open(url)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # -- 插件市场 ---------------------------------------------------------------
    def market_list(self, force: bool = False) -> dict[str, Any]:
        """拉取 awesome-dsh-plugin 精选插件目录（README 解析，10 分钟缓存）。"""
        now = time.time()
        with self._market_lock:
            if (not force and self._market_cache
                    and now - self._market_cache[0] < 600):
                _, items, categories = self._market_cache
                return {"ok": True, "count": len(items),
                        "items": items, "categories": categories}
        try:
            text = _fetch_text(MARKET_URL)
        except Exception as exc:  # noqa: BLE001 —— 网络问题直接报给界面
            return {"ok": False, "error": str(exc)}
        items, categories = _parse_market(text)
        with self._market_lock:
            self._market_cache = (now, items, categories)
        return {"ok": True, "count": len(items), "items": items, "categories": categories}

    def market_install(self, url: str) -> dict[str, Any]:
        """从市场安装插件：浅克隆仓库到临时目录，再走本地安装流程。"""
        url = str(url or "").strip()
        try:
            _validate_url(url)
            if not url.startswith("https://"):
                raise ValueError("插件仓库必须使用 https 链接")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        import shutil
        import subprocess
        import tempfile

        repo = urlparse(url).path.rstrip("/").split("/")[-1].removesuffix(".git")
        tmp = Path(tempfile.mkdtemp(prefix="sha-market-"))
        try:
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", url, str(tmp / repo)],
                capture_output=True, text=True, timeout=180,
                check=False,  # 失败原因在 stderr 里，下面自己判断并转成可读提示
            )
            if proc.returncode != 0:
                return {"ok": False, "error": "git clone 失败: " + (proc.stderr or "").strip()[:300]}
            result = self.install_plugin(str(tmp / repo))
            if result.get("ok"):
                result["note"] = (result.get("note") or "") + f"（来源: {url}）"
            return result
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- 会话 ---------------------------------------------------------------
    def sessions(self) -> dict[str, Any]:
        config = self._config()
        meta = config.get("sessions_meta") or {}
        ids = set(self.host.profile.session_ids()) | set(meta)  # 含已命名但尚无消息的会话
        items = []
        for session_id in ids:
            info = meta.get(session_id) or {}
            items.append({
                "id": session_id,
                "name": info.get("name") or session_id,
                "ws": info.get("ws") or "",
                "updated": info.get("updated") or 0,
            })
        items.sort(key=lambda item: item["updated"], reverse=True)
        return {"ok": True, "sessions": items, "current": self._current_session}

    def session_history(self, session_id: str) -> dict[str, Any]:
        history = [m for m in self.host.profile.load_session(session_id)
                   if m.get("role") in ("user", "assistant")]
        return {"ok": True, "session": session_id, "history": history}

    def new_session(self) -> dict[str, Any]:
        session_id = time.strftime("s-%Y%m%d-%H%M%S")
        reset = self.host.service("new_session")
        if callable(reset):
            reset(session_id)  # 把 id 一并告知对话插件，避免两边各生成一个
        self._current_session = session_id
        self._touch_session(session_id)
        return {"ok": True, "session": session_id}

    def rename_session(self, session_id: str, name: str) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "error": "名称为空"}
        config = self._config()
        meta = dict(config.get("sessions_meta") or {})
        info = dict(meta.get(session_id) or {})
        info["name"] = name
        info["updated"] = time.time()
        meta[session_id] = info
        self.host.profile.update_config(sessions_meta=meta)
        return {"ok": True, "session": session_id, "name": name}

    def chat(self, message: str, session_id: str = "") -> dict[str, Any]:
        session_id = session_id or self._current_session
        with self._lock:
            if session_id != self._current_session:
                reset = self.host.service("new_session")
                if callable(reset):
                    reset(session_id)  # 重建 agent：按目标会话回放历史
                self._current_session = session_id
            ask = self.host.service("ask")
            if not callable(ask):
                return {"ok": False, "error": "对话插件未激活"}
            self.host.emit_agent_event = self._push_event  # 实时步骤 → 界面
            try:
                result = ask(message, session_id=session_id)
            except Exception as exc:  # noqa: BLE001 —— 错误回到界面而不是崩窗口
                return {"ok": False, "error": str(exc)}
            finally:
                self.host.emit_agent_event = None
            result["ok"] = True
            result["session"] = session_id
            self._touch_session(session_id)
            return result

    def _push_event(self, payload: dict[str, Any]) -> None:
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                "window.onAgentEvent && window.onAgentEvent(" + json.dumps(payload) + ")"
            )
        except Exception:  # noqa: BLE001 —— 推送失败不影响对话
            pass

    # -- 模型 ----------------------------------------------------------------
    def get_models(self) -> dict[str, Any]:
        config = self.host.profile.load_config()
        # 明文 API Key 不回传前端（P2 #8）：只给掩码，保存时留空/回传掩码即保持原值
        models = []
        for entry in (config.get("models") or []):
            item = dict(entry)
            item["api_key"] = _mask_key(item.get("api_key"))
            models.append(item)
        return {"ok": True, "models": models,
                "default_model": config.get("default_model", "")}

    def save_models(self, models: list[dict], default_model: str = "") -> dict[str, Any]:
        # 已存在的密钥：前端回传掩码或留空时保持原值，避免被掩码覆盖
        existing = {
            str(m.get("name")): str(m.get("api_key") or "")
            for m in (self.host.profile.load_config().get("models") or [])
        }
        seen: set = set()
        cleaned: list[dict] = []
        for entry in models or []:
            name = str(entry.get("name") or "").strip()
            model = str(entry.get("model") or "").strip()
            api_key = str(entry.get("api_key") or "").strip()
            if api_key == _mask_key(existing.get(name, "")):  # 回传的是掩码
                api_key = existing.get(name, "")
            elif not api_key and existing.get(name):
                api_key = existing[name]                       # 留空 = 保持原值
            if not (name and model and api_key):
                return {"ok": False, "error": f"模型 '{name or '(未命名)'}' 需要 名称/模型/API Key 全部填写"}
            if name in seen:
                return {"ok": False, "error": f"模型名重复: {name}"}
            seen.add(name)
            provider = (entry.get("provider") or "openai").lower()
            if provider not in ("openai", "anthropic"):
                return {"ok": False, "error": f"模型 '{name}' 的 provider 只支持 openai/anthropic"}
            cleaned.append({"name": name, "provider": provider,
                            "base_url": str(entry.get("base_url") or "").strip(),
                            "api_key": api_key, "model": model})
        if cleaned and default_model not in seen:
            default_model = cleaned[0]["name"]
        config = self.host.profile.update_config(models=cleaned, default_model=default_model)
        error = self._rebuild_pool()
        return {"ok": True, "models": [
            {**{k: v for k, v in m.items() if k != "api_key"}, "api_key": _mask_key(m["api_key"])}
            for m in config["models"]
        ], "default_model": config["default_model"], "rebuild_error": error}

    def switch_model(self, name: str) -> dict[str, Any]:
        runtime = self.host.service("models_runtime") or {}
        setter = runtime.get("set")
        if not callable(setter):
            return {"ok": False, "error": "模型插件未激活"}
        message = setter(name)
        ok = not message.startswith("错误")
        return {"ok": ok, "message": message, "model": runtime.get("current", "")}

    def _rebuild_pool(self) -> str:
        """保存配置后热重建模型池（不重启进程）。返回错误信息（无错为空）。

        注意：单个模型构建失败只记录并跳过，**不能在循环里 return** ——
        否则 pool/order/current 停在旧值，出现「配置已落盘、运行时仍是旧池」
        却仍返回 ok:True 的半状态。
        """
        try:
            from .builtins.models.register import _build_llm

            runtime = self.host.service("models_runtime")
            if runtime is None:
                return ""
            config = self.host.profile.load_config()
            pool: dict = {}
            order: list = []
            errors: list = []
            level = (config.get("thinking_level") or "").strip()
            effort = level if level and level != "off" else None
            for entry in config.get("models") or []:
                name = str(entry.get("name") or "").strip()
                if not name:
                    continue
                try:
                    llm = _build_llm(entry)
                except Exception as exc:  # noqa: BLE001 —— 单个模型坏了不影响其余
                    errors.append(f"模型 {name} 构建失败: {exc}")
                    continue
                try:
                    llm.reasoning_effort = effort
                except Exception:  # noqa: BLE001 —— 客户端不支持该属性时忽略
                    pass
                pool[name] = llm
                order.append(name)
            runtime["pool"] = pool
            runtime["order"] = order
            current = config.get("default_model") or (order[0] if order else "")
            runtime["current"] = current if current in pool else ""
            factory = self.host.service("agent_factory")
            if callable(factory) and runtime["current"]:
                try:
                    factory().llm = pool[runtime["current"]]
                except Exception:  # noqa: BLE001 —— agent 未就绪时下次懒构建
                    pass
            return "; ".join(errors)
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    # -- 用量与上下文 ----------------------------------------------------------
    def usage(self) -> dict[str, Any]:
        try:
            llm = getattr(self.host.service("agent_factory")(), "llm", None)
        except Exception:  # noqa: BLE001
            llm = None
        total = dict(getattr(llm, "total_usage", {}) or {})
        return {"ok": True, "usage": total}

    def context_usage(self) -> dict[str, Any]:
        """当前会话的上下文占用：消息/系统提示词/工具/技能分项估算。"""
        try:
            agent = self.host.service("agent_factory")()
        except Exception as exc:  # noqa: BLE001 —— 未配置模型等
            return {"ok": False, "error": str(exc)}
        from nanoagent.memory import estimate_tokens

        llm = getattr(agent, "llm", None)
        window = int(getattr(llm, "context_window", 0) or 128000)
        msgs = 0
        memory = getattr(agent, "memory", None)
        if memory is not None and hasattr(memory, "tokens"):
            msgs = int(memory.tokens(self._current_session))
        # 技能摘要由 enable_skills() 追加进 instructions（marker 之后），
        # 单独拆出来估算；否则「技能」分项恒为几个 token，真实技能文本被并进系统提示词。
        instructions_text = getattr(agent, "instructions", "") or ""
        marker = getattr(agent, "_skills_marker", "") or ""
        skills_text = ""
        if marker and marker in instructions_text:
            instructions_text, skills_text = instructions_text.split(marker, 1)
        instructions = estimate_tokens(instructions_text)
        tools_tokens = 0
        try:
            tools_tokens = estimate_tokens(json.dumps(agent.tools.schemas(), ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
        skills = estimate_tokens(skills_text)
        breakdown = [
            {"name": "消息", "tokens": msgs},
            {"name": "系统工具", "tokens": tools_tokens},
            {"name": "技能", "tokens": skills},
            {"name": "系统提示词", "tokens": instructions},
            {"name": "其他", "tokens": 0},
        ]
        used = sum(item["tokens"] for item in breakdown)
        for item in breakdown:
            item["percent"] = round(item["tokens"] * 100 / used, 1) if used else 0.0
        return {"ok": True, "used": used, "window": window,
                "percent": round(used * 100 / window, 1) if window else 0.0,
                "breakdown": breakdown}

    # -- 右侧面板：工作区浏览 / 终端 / 审查 --------------------------------------
    def ws_tree(self, subpath: str = "") -> dict[str, Any]:
        """列出工作区目录内容（锁定在工作区内部，隐藏点开头条目）。"""
        ws = str(getattr(self.host, "workspace", "") or "")
        if not ws.strip():
            return {"ok": False, "error": "未选择工作区"}
        # 必须 resolve：否则 root 保持相对路径时，target（已 resolve）无法 relative_to(root)，
        # 任何子目录都会被误判为「路径越出工作区」（相对工作区下目录无法展开）。
        root = Path(ws).resolve()
        try:
            target = (root / subpath).resolve() if str(subpath) else root
            target.relative_to(root)
        except ValueError:
            return {"ok": False, "error": "路径越出工作区"}
        if not target.is_dir():
            return {"ok": False, "error": "目录不存在"}
        entries = []
        try:
            items = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            for item in items:
                if item.name.startswith("."):
                    continue
                stat = item.stat()
                entries.append({"name": item.name, "dir": item.is_dir(),
                                "size": 0 if item.is_dir() else stat.st_size,
                                "mtime": int(stat.st_mtime)})
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        rel = target.relative_to(root).as_posix()
        return {"ok": True, "rel": "" if rel == "." else rel, "entries": entries}

    def run_terminal(self, command: str) -> dict[str, Any]:
        """在右侧终端执行命令：复用 shell 工具（权限门 + 工作区 cwd）。"""
        tool = next((t for t in self.host.collect_tools() if t.name == "run_command"), None)
        if tool is None:
            return {"ok": False, "error": "shell 插件未激活"}
        try:
            output = tool.invoke({"command": str(command or ""), "timeout": 120})
        except Exception as exc:  # noqa: BLE001 —— 不让异常越过 js_api 边界
            return {"ok": False, "error": f"命令执行失败: {exc}"}
        return {"ok": True, "output": output}

    def review(self) -> dict[str, Any]:
        """审查工作区改动：git status + git diff。"""
        import locale
        import subprocess

        ws = str(getattr(self.host, "workspace", "") or "")
        if not ws or not Path(ws).is_dir():
            return {"ok": False, "error": "未选择工作区"}

        def decode(raw: bytes) -> str:
            # Windows 中文版 git 输出常为 GBK；依次尝试 utf-8 → 本地代码页 → gbk
            for enc in ("utf-8", locale.getpreferredencoding(False), "gbk"):
                if not enc:
                    continue
                try:
                    return raw.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
            return raw.decode("utf-8", "replace")

        def git(*args: str) -> subprocess.CompletedProcess:
            # 取 bytes 自行解码（多编码兜底）；core.quotepath=false 避免非 ASCII 路径被八进制转义
            return subprocess.run(
                ["git", "-c", "core.quotepath=false", *args],
                cwd=ws, capture_output=True, timeout=30,
                check=False,  # 非零退出（如「不是 git 仓库」）由调用方按语义处理
            )

        status = git("status", "--short")
        if status.returncode != 0:
            return {"ok": False, "error": "当前工作区不是 git 仓库"}
        diff = git("diff", "--unified=3")
        text = ("git status\n" + decode(status.stdout)
                + "\n\ngit diff\n" + (decode(diff.stdout) or "（无未暂存改动）"))
        return {"ok": True, "text": text[:20000]}

    # -- 技能管理 ----------------------------------------------------------------
    def _profile_skills_dir(self) -> Path:
        return Path(self.host.profile.root) / "skills"

    def skills(self) -> dict[str, Any]:
        from nanoagent.skills import SkillRegistry

        registry = SkillRegistry()
        dirs = list(self.host.collect_skill_dirs())
        profile_dir = self._profile_skills_dir()
        if profile_dir.is_dir():
            dirs.append(str(profile_dir))
        for d in dirs:
            registry.add_dir(d)
        profile_root = str(profile_dir.resolve())
        items = []
        for name in registry.names():
            skill = registry.get(name)
            source = str(Path(skill.path).parent if skill.path else "")
            owned = source.startswith(profile_root) if source else False
            items.append({"name": name, "desc": skill.description,
                          "source": "profile" if owned else "插件", "path": source})
        return {"ok": True, "skills": items}

    def install_skill(self, source: str) -> dict[str, Any]:
        import shutil

        src = Path(str(source or "").strip().strip('"'))
        if src.is_file() and src.name.lower() == "skill.md":
            src = src.parent
        if not src.is_dir() or not (src / "SKILL.md").is_file():
            return {"ok": False, "error": "目录需包含 SKILL.md"}
        # src.name 可能是 ''（source='.'、'./'）或 '..'：dest 退化成 skills 目录本身
        # 或其父目录，下面的 rmtree 同名覆盖就会删光整个技能库（审计 H-10a 实证）。
        if not src.name or src.name in (".", ".."):
            return {"ok": False, "error": "源目录不能是 '.'、'..' 或根路径，请选择具体的技能文件夹"}
        skills_root = self._profile_skills_dir().resolve()
        dest = (skills_root / src.name).resolve()
        if dest == skills_root or not dest.is_relative_to(skills_root):
            return {"ok": False, "error": "安装目标越出了 profile 技能目录"}
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)  # 同名覆盖（已确认仍在 skills_root 内）
        shutil.copytree(src, dest)
        return {"ok": True, "name": src.name}

    def remove_skill(self, name: str) -> dict[str, Any]:
        import shutil

        dest = self._profile_skills_dir() / str(name or "")
        if not dest.resolve().is_dir() or not dest.resolve().is_relative_to(
                self._profile_skills_dir().resolve()):
            return {"ok": False, "error": "只能移除安装在 profile 的技能"}
        shutil.rmtree(dest.resolve())
        return {"ok": True, "name": name}

    # -- 插件 ----------------------------------------------------------------
    def plugins(self) -> dict[str, Any]:
        plugins_root = self.host.profile.plugins_dir.resolve()
        items = []
        for plugin in self.host.list():
            if plugin["state"] == "DISPOSED":  # 已移除的插件不再展示
                continue
            # 只有 profile 目录内的插件才允许移除（内置插件不提供移除按钮）
            record = self.host.plugins.get(plugin["name"])
            removable = False
            if record is not None:
                try:
                    removable = Path(record.path).resolve().is_relative_to(plugins_root)
                except OSError:
                    removable = False
            items.append({
                "name": plugin["name"], "state": plugin["state"],
                "provided": {k: v for k, v in plugin["provided"].items() if v},
                "skipped": plugin["skipped"], "error": plugin["error"],
                "removable": removable,
            })
        return {"ok": True, "plugins": items}

    def install_plugin(self, source: str) -> dict[str, Any]:
        try:
            dest = self.host.profile.install_plugin(source)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        # H-06：mount 以 manifest 里的 name 登记，activate 必须用同一个标识，
        # 否则目录名 ≠ manifest name 时会 KeyError（此前被吞成 note 且仍返回 ok:true）。
        try:
            record = self.host.mount(dest)
            self.host.activate(record.name)
        except Exception as exc:  # noqa: BLE001 —— 已复制到 profile，重启后可装载
            return {
                "ok": False,
                "name": dest.name,
                "error": f"{dest.name} 已复制，但热激活失败（重启后生效）: {exc}",
            }
        if record.state != ACTIVE:
            # activate 内部把单插件失败降级为 FAILED（不外抛），这里如实上报
            return {
                "ok": False,
                "name": record.name,
                "error": f"{record.name} 已复制，但激活失败（重启后生效）: {record.error}",
            }
        return {"ok": True, "name": record.name, "note": "已热激活"}

    def remove_plugin(self, name: str) -> dict[str, Any]:
        # 记录里的目录名可能与插件名（manifest name）不同，必须按 record.path 删除，
        # 否则会去删 <plugins>/<manifest-name> 这个不存在的目录，留下半卸载状态。
        record = self.host.plugins.get(name)
        try:
            self.host.deactivate(name)
        except Exception:  # noqa: BLE001 —— 未激活也允许删除
            pass
        target = record.path if record is not None else (self.host.profile.plugins_dir / name)
        if not self.host.profile.remove_plugin(target):
            return {"ok": False, "name": name, "error": f"未找到可移除的插件目录: {name}"}
        self.host.plugins.pop(name, None)
        return {"ok": True, "name": name}


# ---------------------------------------------------------------------------
# 渲染层（仅 UI；所有能力走 window.pywebview.api）
# 视觉对齐 dsh-desktop：近黑工作台、左侧栏、居中输入卡片、DeepSeek 蓝点缀
# ---------------------------------------------------------------------------

UI_HTML = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>卅 harness</title><style>
:root {
  --bg: #0f0f0f; --panel: #161616; --elev: #1d1d1d; --hover: #242424;
  --line: #2a2a2a; --line-soft: #222222;
  --fg: #ececec; --dim: #8f8f8f; --faint: #5c5c5c;
  --accent: #4d6bfe; --accent-soft: rgba(77,107,254,.14);
  --ok: #58a66b; --err: #e07a6c;
  --bubble: #262626; --scroll: #333; --shadow: rgba(0,0,0,.5);
  --radius: 12px;
}
html[data-theme="light"] {
  --bg: #f7f7f8; --panel: #ffffff; --elev: #f2f2f4; --hover: #e9e9ec;
  --line: #e1e1e5; --line-soft: #ececee;
  --fg: #1c1c1e; --dim: #6b6b72; --faint: #a0a0a8;
  --accent-soft: rgba(77,107,254,.10);
  --bubble: #e8e8ee; --scroll: #d2d2d8; --shadow: rgba(0,0,0,.12);
}
@media (prefers-color-scheme: light) {
  html[data-theme="system"] {
    --bg: #f7f7f8; --panel: #ffffff; --elev: #f2f2f4; --hover: #e9e9ec;
    --line: #e1e1e5; --line-soft: #ececee;
    --fg: #1c1c1e; --dim: #6b6b72; --faint: #a0a0a8;
    --accent-soft: rgba(77,107,254,.10);
    --bubble: #e8e8ee; --scroll: #d2d2d8; --shadow: rgba(0,0,0,.12);
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin:0; font-family:'Segoe UI','Microsoft YaHei',system-ui,sans-serif;
       background:var(--bg); color:var(--fg); overflow:hidden;
       -webkit-font-smoothing: antialiased; }
::-webkit-scrollbar { width:8px; height:8px; }
::-webkit-scrollbar-thumb { background:var(--scroll); border-radius:4px; }
::-webkit-scrollbar-thumb:hover { background:var(--dim); }
::-webkit-scrollbar-track { background:transparent; }
.app { display:flex; height:100vh; }

/* ---------- 侧栏 ---------- */
aside { width:260px; min-width:260px; background:var(--panel);
        border-right:1px solid var(--line-soft); display:flex; flex-direction:column;
        padding:14px 12px; }
.brand { display:flex; align-items:center; gap:10px; padding:2px 6px 14px; }
.logo { width:32px; height:32px; border-radius:9px; background:var(--fg); color:var(--bg);
        display:flex; align-items:center; justify-content:center;
        font-weight:800; font-size:17px; font-family:Georgia,'Times New Roman',serif; }
.brand b { font-size:13.5px; letter-spacing:.06em; display:block; }
.brand small { color:var(--faint); font-size:11px; letter-spacing:.04em; }
.new-btn { display:flex; align-items:center; justify-content:center; gap:8px;
           width:100%; padding:10px 0; border-radius:var(--radius); cursor:pointer;
           background:var(--elev); border:1px solid var(--line); color:var(--fg);
           font-size:13.5px; transition:background .15s, border-color .15s; }
.new-btn:hover { background:var(--hover); border-color:#3a3a3a; }
.side-label { color:var(--faint); font-size:11px; letter-spacing:.1em; margin:16px 8px 6px; }
.side-list { flex:1; overflow-y:auto; margin:0 -4px; }
.ws-group { margin-bottom:2px; }
.ws-head { display:flex; align-items:center; gap:7px; padding:6px 10px; border-radius:8px;
           color:var(--dim); font-size:12.5px; cursor:pointer; user-select:none; }
.ws-head:hover { background:var(--hover); }
.ws-caret { flex:none; font-size:9px; color:var(--faint); transition:transform .15s;
            width:10px; text-align:center; }
.ws-group.collapsed .ws-caret { transform:rotate(-90deg); }
.ws-group.collapsed .s-row { display:none; }
.ws-head .ws-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.row-menu { border:none; background:transparent; color:var(--faint); font-size:14px;
            cursor:pointer; border-radius:6px; padding:0 6px; visibility:hidden; }
.ws-head:hover .row-menu, .s-row:hover .row-menu { visibility:visible; }
.row-menu:hover { color:var(--fg); background:#333; }
.s-row { display:flex; align-items:center; gap:8px; padding:6px 10px 6px 26px;
         border-radius:8px; cursor:pointer; font-size:13px; color:var(--dim); }
.s-row:hover { background:var(--hover); color:var(--fg); }
.s-row.active { background:var(--accent-soft); color:var(--fg); }
.s-row .s-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.s-row .s-time { color:var(--faint); font-size:11px; flex:none; }
.side-bottom { border-top:1px solid var(--line-soft); padding-top:10px; margin-top:8px; }
.side-action { display:flex; align-items:center; gap:8px; width:100%; padding:8px 10px;
               border:none; background:transparent; color:var(--dim); font-size:13px;
               border-radius:8px; cursor:pointer; }
.side-action:hover { background:var(--hover); color:var(--fg); }
.side-hint { color:var(--faint); font-size:11px; padding:6px 10px 0; line-height:1.5; }

/* ---------- 主区 ---------- */
main { flex:1; display:flex; flex-direction:column; min-width:0; min-height:0; }
#hero { display:none; flex:1; flex-direction:column; align-items:center;
        justify-content:center; text-align:center; padding-bottom:8px; min-height:0; }
#hero .glyph { font-family:Georgia,'Times New Roman',serif; font-size:52px; font-weight:800;
               width:84px; height:84px; border-radius:22px; background:var(--elev);
               border:1px solid var(--line); display:flex; align-items:center;
               justify-content:center; margin-bottom:22px; }
#hero h1 { font-family:Georgia,'Times New Roman',serif; font-weight:700;
           font-size:34px; margin:0 0 10px; letter-spacing:.01em; }
#hero p { color:var(--dim); font-size:13.5px; margin:0; max-width:420px; line-height:1.7; }
#log { flex:1; overflow-y:auto; padding:26px 24px 10px; }
.thread { max-width:760px; margin:0 auto; }
.msg { position:relative; margin:0 0 18px; font-size:14px; line-height:1.75;
       word-break:break-word; user-select:text; -webkit-user-select:text; cursor:text; }
.msg ::selection, .msg::selection { background:var(--accent-soft); }
.msg.user { background:var(--bubble); border-radius:14px; padding:10px 16px;
            max-width:78%; margin-left:auto; width:fit-content; white-space:pre-wrap; }
.msg.bot { white-space:pre-wrap; }
.msg.bot code { background:var(--elev); border:1px solid var(--line); border-radius:5px;
                padding:1px 6px; font-family:Consolas,monospace; font-size:12.5px; }
.msg.bot .bold { font-weight:700; }
.msg.error { color:var(--err); }
.meta { color:var(--faint); font-size:11.5px; margin-top:6px; }
.meta .toolchip { background:var(--elev); border:1px solid var(--line); border-radius:6px;
                  padding:1px 8px; margin-right:5px; display:inline-block; font-size:11px; }
.reasoning { border:1px solid var(--line-soft); border-radius:10px; margin-bottom:10px;
             background:var(--panel); overflow:hidden; }
.r-head { padding:6px 12px; color:var(--faint); font-size:12px; cursor:pointer;
          user-select:none; }
.r-head:hover { color:var(--dim); }
.r-body { display:none; padding:2px 12px 10px; color:var(--dim); font-size:12.5px;
          line-height:1.7; white-space:pre-wrap; border-top:1px dashed var(--line-soft); }
.reasoning.open .r-body { display:block; }
.thinking { display:inline-flex; gap:5px; padding:6px 0; }
.thinking i { width:6px; height:6px; border-radius:50%; background:var(--dim);
              animation:blink 1.2s infinite; }
.thinking i:nth-child(2) { animation-delay:.2s; }
.thinking i:nth-child(3) { animation-delay:.4s; }
@keyframes blink { 0%,70%,100% { opacity:.25; } 35% { opacity:1; } }

/* ---------- 输入卡片 ---------- */
.composer-wrap { padding:10px 24px 6px; }
.ws-bar { max-width:760px; margin:0 auto 8px; position:relative; display:none; }
#wsBtn { display:inline-flex; align-items:center; gap:7px; border:1px solid var(--line);
         background:var(--elev); color:var(--dim); border-radius:999px;
         padding:5px 14px; font-size:12.5px; cursor:pointer; max-width:340px; }
#wsBtn:hover { color:var(--fg); border-color:#3d3d3d; }
#wsBtn .ws-cur { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#wsMenu { display:none; position:absolute; top:36px; left:0; z-index:6; min-width:260px;
          max-width:360px; background:var(--panel); border:1px solid var(--line);
          border-radius:12px; padding:6px; box-shadow:0 8px 28px var(--shadow); }
#wsMenu.open { display:block; }
#wsMenu .ws-menu-label { color:var(--faint); font-size:10.5px; letter-spacing:.08em;
                         padding:4px 10px; }
#wsMenu button { display:flex; align-items:center; gap:8px; width:100%; padding:7px 12px;
                 border:none; background:transparent; color:var(--dim); font-size:12.5px;
                 border-radius:8px; cursor:pointer; text-align:left; }
#wsMenu button:hover { background:var(--hover); color:var(--fg); }
#wsMenu button.sel { color:var(--accent); }
#wsMenu .ws-path { color:var(--faint); font-size:11px; overflow:hidden;
                   text-overflow:ellipsis; white-space:nowrap; flex:1; }
.composer { max-width:760px; margin:0 auto; background:var(--elev);
            border:1px solid var(--line); border-radius:16px; padding:12px 14px 10px;
            transition:border-color .15s; position:relative; }
.composer:focus-within { border-color:#3d3d3d; }
#input { width:100%; background:transparent; border:none; outline:none; resize:none;
         color:var(--fg); font-size:14px; line-height:1.6; font-family:inherit;
         min-height:24px; max-height:180px; }
#input::placeholder { color:var(--faint); }
.composer-row { display:flex; align-items:center; gap:8px; margin-top:8px; }
.badge { border:1px solid var(--line); background:transparent; color:var(--dim);
         border-radius:999px; padding:3px 11px; font-size:11.5px; cursor:pointer; }
.badge:hover { color:var(--fg); border-color:#3d3d3d; }
.badge.allow { color:var(--ok); border-color:rgba(88,166,107,.4); }
.badge.deny  { color:var(--err); border-color:rgba(224,122,108,.4); }
#ctxBtn { border:1px solid var(--line); background:transparent; color:var(--dim);
          border-radius:999px; padding:3px 11px; font-size:11.5px; cursor:pointer; }
#ctxBtn:hover { color:var(--fg); border-color:#3d3d3d; }
.flex1 { flex:1; }
#modelSel, #thinkSel { background:transparent; border:1px solid var(--line); color:var(--dim);
            border-radius:8px; padding:4px 8px; font-size:12px; max-width:170px;
            outline:none; cursor:pointer; }
#modelSel:focus, #thinkSel:focus { color:var(--fg); }
#modelSel option, #thinkSel option { background:var(--elev); color:var(--fg); }
.send { width:34px; height:34px; border-radius:50%; border:none; cursor:pointer;
        background:var(--accent); color:#fff; font-size:15px; line-height:1;
        display:flex; align-items:center; justify-content:center;
        transition:opacity .15s, transform .1s; }
.send:hover { opacity:.88; }
.send:active { transform:scale(.94); }
.send:disabled { opacity:.4; cursor:default; }
.send.busy { background:transparent; border:2px solid var(--line);
             border-top-color:var(--accent); animation:spin .8s linear infinite;
             color:transparent; }
@keyframes spin { to { transform:rotate(360deg); } }
#usageLine { max-width:760px; margin:4px auto 0; color:var(--faint); font-size:11px;
             text-align:right; padding-right:4px; }
.composer-foot { padding:2px 24px 12px; }

/* ---------- 弹出菜单 / 卡片 ---------- */
.perm-menu { display:none; position:absolute; left:14px; bottom:52px; z-index:5;
             background:var(--panel); border:1px solid var(--line); border-radius:12px;
             padding:6px; min-width:210px; box-shadow:0 8px 28px rgba(0,0,0,.5); }
.perm-menu.open { display:block; }
.perm-menu h5 { margin:4px 8px; color:var(--faint); font-size:10.5px; letter-spacing:.08em; }
.perm-menu button { display:flex; align-items:center; gap:8px; width:100%; padding:7px 12px;
                    border:none; background:transparent; color:var(--dim); font-size:12.5px;
                    border-radius:8px; cursor:pointer; text-align:left; }
.perm-menu button:hover { background:var(--hover); color:var(--fg); }
.perm-menu button.sel { color:var(--accent); }
#ctxCard { display:none; position:absolute; right:14px; bottom:52px; z-index:5; width:320px;
           background:var(--panel); border:1px solid var(--line); border-radius:14px;
           padding:14px 16px; box-shadow:0 8px 28px rgba(0,0,0,.5); }
#ctxCard.open { display:block; }
#ctxCard h4 { margin:0 0 10px; font-size:13px; display:flex; align-items:baseline; gap:8px; }
#ctxCard h4 small { color:var(--faint); font-size:11px; margin-left:auto; }
.ctx-bar { height:6px; border-radius:3px; background:var(--hover); overflow:hidden; }
.ctx-bar i { display:block; height:100%; background:var(--accent); border-radius:3px; }
.ctx-rows { margin-top:10px; }
.ctx-row { display:flex; align-items:center; gap:8px; padding:3px 0; font-size:12px;
           color:var(--dim); }
.ctx-row .dot { width:7px; height:7px; border-radius:50%; background:var(--accent); }
.ctx-row .dot.dim { background:#3b4a86; }
.ctx-row .name { flex:1; }
.ctx-row .pct { color:var(--fg); }
#ctxCard .hint { margin-top:10px; }

/* ---------- 实时步骤（模型运作过程） ---------- */
.step { display:flex; align-items:center; gap:8px; color:var(--dim); font-size:12.5px;
        padding:4px 0; user-select:text; }
.step .s-icon { flex:none; }
.step .s-text { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;
                white-space:nowrap; }
.step.pending .s-icon { animation:spin 1s linear infinite; display:inline-block; }
.step.result { color:var(--faint); font-size:11.5px; padding-left:22px; }

/* ---------- 右侧面板 ---------- */
aside.right { width:360px; min-width:360px; background:var(--panel);
              border-left:1px solid var(--line-soft); display:flex;
              flex-direction:column; }
body.panel-hidden aside.right { display:none; }
.rp-tabs { display:flex; align-items:center; gap:2px; padding:8px 8px;
           border-bottom:1px solid var(--line-soft); }
.rp-tab { border:none; background:transparent; color:var(--dim); font-size:12px;
          padding:5px 10px; border-radius:8px; cursor:pointer; }
.rp-tab:hover { color:var(--fg); background:var(--hover); }
.rp-tab.active { background:var(--elev); color:var(--fg); }
.rp-tabs .rp-close { margin-left:auto; background:transparent; border:none;
                     color:var(--faint); font-size:13px; cursor:pointer;
                     padding:3px 8px; border-radius:8px; }
.rp-tabs .rp-close:hover { background:var(--hover); color:var(--fg); }
.rp-body { flex:1; min-height:0; display:none; flex-direction:column; }
.rp-body.active { display:flex; }
.rp-pane { flex:1; overflow-y:auto; padding:12px; font-size:13px; }
#panelToggle.side-action.off { color:var(--faint); }

/* 工作区树 */
.ws-nav { display:flex; align-items:center; gap:6px; padding:8px 12px;
          border-bottom:1px solid var(--line-soft); }
.ws-nav .crumb { flex:1; color:var(--dim); font-size:12px; overflow:hidden;
                 text-overflow:ellipsis; white-space:nowrap; direction:rtl;
                 text-align:left; }
.mini-btn { border:1px solid var(--line); background:transparent; color:var(--dim);
            border-radius:7px; font-size:11.5px; padding:3px 9px; cursor:pointer; }
.mini-btn:hover { color:var(--fg); border-color:#3d3d3d; }
.tree-row { display:flex; align-items:center; gap:8px; padding:5px 10px;
            border-radius:7px; cursor:pointer; color:var(--dim); font-size:12.5px; }
.tree-row:hover { background:var(--hover); color:var(--fg); }
.tree-row .t-name { flex:1; overflow:hidden; text-overflow:ellipsis;
                    white-space:nowrap; }
.tree-row .t-size { color:var(--faint); font-size:11px; flex:none; }

/* 终端 */
.term-out { flex:1; overflow-y:auto; background:var(--bg); margin:0; padding:12px;
            font-family:Consolas,monospace; font-size:12px; line-height:1.6;
            color:var(--dim); white-space:pre-wrap; word-break:break-all;
            user-select:text; -webkit-user-select:text; }
.term-row { display:flex; gap:6px; padding:8px 12px; border-top:1px solid var(--line-soft); }
.term-row input { flex:1; background:var(--bg); border:1px solid var(--line);
                  border-radius:8px; color:var(--fg); padding:7px 10px;
                  font-family:Consolas,monospace; font-size:12.5px; outline:none; }

/* 浏览器 */
.br-bar { display:flex; gap:6px; padding:8px 12px; border-bottom:1px solid var(--line-soft); }
.br-bar input { flex:1; background:var(--bg); border:1px solid var(--line);
                border-radius:8px; color:var(--fg); padding:7px 10px; font-size:12.5px;
                outline:none; }
#browserFrame { flex:1; border:none; background:#fff; width:100%; }

/* 审查 */
.review-out { flex:1; overflow-y:auto; background:var(--bg); margin:0; padding:12px;
              font-family:Consolas,monospace; font-size:12px; line-height:1.6;
              color:var(--dim); white-space:pre-wrap; word-break:break-all;
              user-select:text; -webkit-user-select:text; }
.diff-add { color:var(--ok); }
.diff-del { color:var(--err); }

/* 辅助对话 */
#auxThread { flex:1; overflow-y:auto; padding:12px; }
#auxThread .msg { font-size:13px; margin-bottom:12px; }
#auxThread .msg.user { font-size:13px; padding:8px 13px; }
.aux-row { display:flex; align-items:center; gap:8px; padding:8px 12px;
           border-top:1px solid var(--line-soft); }
.aux-row input { flex:1; background:var(--bg); border:1px solid var(--line);
                 border-radius:10px; color:var(--fg); padding:8px 12px;
                 font-size:12.5px; outline:none; }
.aux-hint { color:var(--faint); font-size:10.5px; padding:0 12px 8px; }

/* ---------- 对话框 ---------- */
#dlgOverlay { position:fixed; inset:0; background:rgba(0,0,0,.6); display:none;
               align-items:center; justify-content:center; z-index:20; }
#dlg { background:var(--panel); border:1px solid var(--line); border-radius:16px;
       width:min(440px, 90vw); padding:20px; }
#dlg h3 { margin:0 0 10px; font-size:15px; }
#dlg .msg-text { color:var(--dim); font-size:13px; line-height:1.7; white-space:pre-wrap; }
#dlg input { width:100%; margin-top:12px; background:var(--bg); border:1px solid var(--line);
             border-radius:10px; color:var(--fg); padding:9px 12px; font-size:13px; outline:none; }
#dlg .dlg-row { display:flex; justify-content:flex-end; gap:10px; margin-top:16px; }

/* ---------- 设置弹窗 ---------- */
#overlay { position:fixed; inset:0; background:rgba(0,0,0,.6); display:none;
           align-items:center; justify-content:center; z-index:10; }
#modal { background:var(--panel); border:1px solid var(--line); border-radius:16px;
         width:min(760px, 92vw); max-height:86vh; display:flex; flex-direction:column;
         overflow:hidden; }
.tabs { display:flex; align-items:center; gap:4px; padding:12px 16px;
        border-bottom:1px solid var(--line-soft); }
.tab { border:none; background:transparent; color:var(--dim); font-size:13px;
       padding:6px 14px; border-radius:8px; cursor:pointer; }
.tab:hover { color:var(--fg); }
.tab.active { background:var(--elev); color:var(--fg); }
.tabs .close { margin-left:auto; background:transparent; border:none; color:var(--dim);
               font-size:15px; cursor:pointer; padding:4px 8px; border-radius:8px; }
.tabs .close:hover { background:var(--hover); color:var(--fg); }
.tab-body { padding:16px; overflow-y:auto; }
.hint { color:var(--faint); font-size:12px; line-height:1.6; }
.note-ok { color:var(--ok); font-size:12px; }
.note-err { color:var(--err); font-size:12px; }
.model-card { background:var(--elev); border:1px solid var(--line-soft); border-radius:12px;
              padding:12px; margin:10px 0; }
.model-grid { display:grid; grid-template-columns:1fr 120px 1fr; gap:8px; }
.model-grid2 { display:grid; grid-template-columns:1fr 1fr auto auto; gap:8px; margin-top:8px;
               align-items:end; }
.field input, .field select { width:100%; background:var(--bg); border:1px solid var(--line);
         border-radius:8px; color:var(--fg); padding:7px 10px; font-size:12.5px; outline:none; }
.field input:focus, .field select:focus { border-color:#3d3d3d; }
.field label { display:block; color:var(--faint); font-size:10.5px; margin-bottom:4px;
               letter-spacing:.06em; }
.radio-default { display:flex; align-items:center; gap:6px; color:var(--dim);
                 font-size:12px; cursor:pointer; padding:0 4px 8px; }
.radio-default input { accent-color: var(--accent); }
.icon-btn { background:transparent; border:1px solid var(--line); color:var(--dim);
            border-radius:8px; padding:6px 12px; font-size:12px; cursor:pointer; }
.icon-btn:hover { color:var(--err); border-color:rgba(224,122,108,.4); }
.modal-footer { display:flex; align-items:center; gap:10px; margin-top:14px; }
.primary { background:var(--accent); border:none; color:#fff; border-radius:10px;
           padding:8px 22px; font-size:13px; cursor:pointer; }
.primary:hover { opacity:.88; }
.ghost { background:transparent; border:1px solid var(--line); color:var(--dim);
         border-radius:10px; padding:8px 18px; font-size:13px; cursor:pointer; }
.ghost:hover { color:var(--fg); border-color:#3d3d3d; }
.plugin-row { display:flex; align-items:center; gap:10px; padding:11px 12px;
              border:1px solid var(--line-soft); background:var(--elev);
              border-radius:12px; margin:8px 0; }
.plugin-row .pname { font-size:13px; }
.plugin-state { font-size:10.5px; border-radius:999px; padding:2px 9px; letter-spacing:.05em; }
.plugin-state.ACTIVE { color:var(--ok); background:rgba(88,166,107,.12); }
.plugin-state.FAILED { color:var(--err); background:rgba(224,122,108,.12); }
.plugin-provided { color:var(--faint); font-size:11px; flex:1; white-space:nowrap;
                   overflow:hidden; text-overflow:ellipsis; }
.install-row { display:flex; gap:8px; margin-top:14px; }
.install-row input { flex:1; background:var(--bg); border:1px solid var(--line);
                     border-radius:10px; color:var(--fg); padding:8px 12px;
                     font-size:12.5px; outline:none; }

/* ---------- 插件市场 ---------- */
.market-toolbar { display:flex; gap:8px; align-items:center; margin:10px 0; }
.market-toolbar input { flex:1; background:var(--bg); border:1px solid var(--line);
                        border-radius:10px; color:var(--fg); padding:8px 12px;
                        font-size:12.5px; outline:none; }
.market-toolbar select { background:var(--bg); border:1px solid var(--line); color:var(--fg);
                         border-radius:10px; padding:8px 10px; font-size:12.5px;
                         outline:none; cursor:pointer; max-width:180px; }
.market-row { display:flex; align-items:flex-start; gap:10px; padding:10px 12px;
              border:1px solid var(--line-soft); background:var(--elev);
              border-radius:12px; margin:8px 0; }
.market-row .m-main { flex:1; min-width:0; }
.market-row .m-name { font-size:13px; color:var(--fg); word-break:break-all; }
.market-row .m-name a { color:inherit; text-decoration:none; }
.market-row .m-desc { color:var(--dim); font-size:12px; margin-top:3px; line-height:1.55;
                      word-break:break-word; }
.market-row .m-cat { flex:none; color:var(--accent); background:var(--accent-soft);
                     border-radius:999px; padding:2px 10px; font-size:10.5px;
                     white-space:nowrap; margin-top:2px; }
.market-row .m-install { flex:none; margin-top:2px; }
.market-row .m-install button { background:var(--accent); border:none; color:#fff;
                                border-radius:8px; padding:5px 14px; font-size:12px;
                                cursor:pointer; }
.market-row .m-install button:disabled { opacity:.45; cursor:default; }
</style></head><body>
<div class="app">
  <aside>
    <div class="brand">
      <div class="logo">卅</div>
      <div><b>卅 HARNESS</b><small>LOCAL-FIRST AGENT</small></div>
    </div>
    <button class="new-btn" onclick="newSessionFlow()">＋ 新会话</button>
    <div class="side-label">项目</div>
    <div id="wsTree" class="side-list"></div>
    <div class="side-bottom">
      <button class="side-action" id="themeBtn" onclick="cycleTheme()" title="切换外观">🌙 外观: 深色</button>
      <button class="side-action" id="panelToggle" onclick="togglePanel()" title="显示/隐藏右侧面板">◧ 面板</button>
      <button class="side-action" onclick="openSettings()">⚙ 模型与插件</button>
      <div class="side-hint" id="statusHint"></div>
    </div>
  </aside>
  <main>
    <div id="hero">
      <div class="glyph">卅</div>
      <h1>准备好，做真正的事。</h1>
      <p>一切皆插件的本地 agent —— 对话、读写文件、跑命令、接 MCP、装插件。<br>在下方描述你想做什么。</p>
    </div>
    <div id="log"><div class="thread" id="thread"></div></div>
    <div class="composer-wrap">
      <div class="ws-bar" id="wsBar">
        <button id="wsBtn" onclick="toggleWsMenu(event)">
          📁 <span class="ws-cur" id="wsCur">选择工作区</span><span>▾</span>
        </button>
        <div id="wsMenu"></div>
      </div>
      <div class="composer">
        <textarea id="input" rows="1"
          placeholder="描述你想做的事…（Enter 发送，Shift+Enter 换行）"></textarea>
        <div class="composer-row">
          <button id="permBadge" class="badge" title="执行权限" onclick="togglePermMenu()"></button>
          <span class="flex1"></span>
          <button id="ctxBtn" title="上下文占用" onclick="toggleCtxCard()">…</button>
          <select id="thinkSel" title="思考级别（映射 reasoning_effort）"></select>
          <select id="modelSel" title="当前模型"></select>
          <button id="send" class="send" onclick="send()">↑</button>
        </div>
        <div id="permMenu" class="perm-menu">
          <h5>SHELL 命令</h5>
          <button data-kind="shell" data-mode="ask" onclick="pickPerm('shell','ask')">💬 每次询问</button>
          <button data-kind="shell" data-mode="allow" onclick="pickPerm('shell','allow')">✅ 允许执行</button>
          <button data-kind="shell" data-mode="deny" onclick="pickPerm('shell','deny')">🚫 禁止执行</button>
          <h5>文件写入</h5>
          <button data-kind="fs" data-mode="ask" onclick="pickPerm('fs','ask')">💬 每次询问</button>
          <button data-kind="fs" data-mode="allow" onclick="pickPerm('fs','allow')">✅ 允许写入</button>
          <button data-kind="fs" data-mode="deny" onclick="pickPerm('fs','deny')">🚫 禁止写入</button>
        </div>
        <div id="ctxCard"></div>
      </div>
    </div>
    <div class="composer-foot"><div id="usageLine"></div></div>
  </main>
  <aside class="right" id="rightPanel">
    <div class="rp-tabs">
      <button class="rp-tab active" data-rp="aux" onclick="switchPanel('aux')">💬 辅助</button>
      <button class="rp-tab" data-rp="ws" onclick="switchPanel('ws')">📁 工作区</button>
      <button class="rp-tab" data-rp="term" onclick="switchPanel('term')">⌨ 终端</button>
      <button class="rp-tab" data-rp="browser" onclick="switchPanel('browser')">🌐 浏览器</button>
      <button class="rp-tab" data-rp="review" onclick="switchPanel('review')">🔍 审查</button>
      <button class="rp-close" onclick="togglePanel()" title="收起面板">✕</button>
    </div>

    <div class="rp-body active" id="rp-aux">
      <div id="auxThread"></div>
      <div class="aux-row">
        <input id="auxInput" placeholder="提出后续修改要求…（Enter 发送）">
        <button id="auxSend" class="send" onclick="auxSend()">↑</button>
      </div>
      <div class="aux-hint">独立辅助对话 —— 与左侧主会话互不影响，共用当前模型与工具。</div>
    </div>

    <div class="rp-body" id="rp-ws">
      <div class="ws-nav">
        <button class="mini-btn" onclick="wsUp()">⬆ 上级</button>
        <span class="crumb" id="wsCrumb">/</span>
        <button class="mini-btn" onclick="loadWsTree()">⟳</button>
      </div>
      <div class="rp-pane" id="wsTreePane"></div>
    </div>

    <div class="rp-body" id="rp-term">
      <pre class="term-out" id="termOut">在此输入命令，在工作区目录执行。</pre>
      <div class="term-row">
        <input id="termInput" placeholder="命令，如 git status" spellcheck="false">
        <button class="mini-btn" onclick="termRun()">执行</button>
      </div>
    </div>

    <div class="rp-body" id="rp-browser">
      <div class="br-bar">
        <input id="brUrl" placeholder="输入网址，如 https://example.com">
        <button class="mini-btn" onclick="brGo()">打开</button>
        <button class="mini-btn" onclick="brExternal()">系统浏览器</button>
      </div>
      <iframe id="browserFrame" src="about:blank"></iframe>
      <div class="aux-hint" style="padding-top:6px">部分站点（如 GitHub）禁止内嵌，请用「系统浏览器」打开。</div>
    </div>

    <div class="rp-body" id="rp-review">
      <div class="ws-nav">
        <span class="crumb">工作区改动（git status + diff）</span>
        <button class="mini-btn" onclick="loadReview()">⟳ 生成</button>
      </div>
      <pre class="review-out" id="reviewOut">点击右上角「生成」查看当前工作区的未提交改动。</pre>
    </div>
  </aside>
</div>

<div id="dlgOverlay">
  <div id="dlg">
    <h3 id="dlgTitle"></h3>
    <div class="msg-text" id="dlgMsg"></div>
    <input id="dlgInput" style="display:none">
    <div class="dlg-row">
      <button class="ghost" id="dlgCancel">取消</button>
      <button class="primary" id="dlgOk">确定</button>
    </div>
  </div>
</div>

<div id="overlay">
  <div id="modal">
    <div class="tabs">
      <button class="tab active" data-tab="models" onclick="switchTab('models')">模型</button>
      <button class="tab" data-tab="plugins" onclick="switchTab('plugins')">插件</button>
      <button class="tab" data-tab="skills" onclick="switchTab('skills')">技能</button>
      <button class="tab" data-tab="market" onclick="switchTab('market')">市场</button>
      <button class="close" onclick="closeSettings()">✕</button>
    </div>
    <div class="tab-body">
      <div id="tab-models">
        <div class="hint">至少填写 名称 / 模型 / API Key；provider 仅支持 openai / anthropic。保存后立即生效，无需重启。</div>
        <div id="cards"></div>
        <div class="modal-footer">
          <button class="ghost" onclick="addCard()">＋ 添加模型</button>
          <span class="flex1"></span>
          <span id="saveNote"></span>
          <button class="primary" onclick="saveSettings()">保存</button>
        </div>
      </div>
      <div id="tab-plugins" style="display:none">
        <div class="hint">插件目录需含 plugin.json 与 register.py。输入本地路径即可安装；点击条目右侧移除。</div>
        <div id="pluginCards"></div>
        <div class="install-row">
          <input id="pluginPath" placeholder="本地插件目录路径，如 D:\plugins\my-plugin">
          <button class="ghost" onclick="installPlugin()">安装</button>
        </div>
        <div class="modal-footer"><span id="pluginNote"></span></div>
      </div>
      <div id="tab-skills" style="display:none">
        <div class="hint">技能即 SKILL.md（Agent Skills 通用格式）：模型按需加载全文。
          插件自带的技能只读；安装到 profile 的技能可以移除。</div>
        <div id="skillCards"></div>
        <div class="install-row">
          <input id="skillPath" placeholder="本地技能目录路径（内含 SKILL.md），如 D:\skills\my-skill">
          <button class="ghost" onclick="installSkill()">安装</button>
        </div>
        <div class="modal-footer"><span id="skillNote"></span></div>
      </div>
      <div id="tab-market" style="display:none">
        <div class="hint">插件市场来自 <b>awesome-dsh-plugin</b> 社区精选目录（DeepSeek Harness 生态）。
          安装即浅克隆仓库到当前 profile：原生格式（plugin.json + register.py）完整生效；
          dsh 的 npm/TS 插件只能识别其中的技能 / MCP / 配置等声明层，代码体无法执行，结果以插件列表为准。</div>
        <div class="market-toolbar">
          <input id="marketQuery" placeholder="搜索插件名称 / 描述…" oninput="renderMarket()">
          <select id="marketCat" onchange="renderMarket()"></select>
          <button class="ghost" onclick="loadMarket(true)">刷新</button>
        </div>
        <div id="marketList" style="max-height:46vh; overflow-y:auto;"></div>
        <div class="modal-footer"><span id="marketNote"></span></div>
      </div>
    </div>
  </div>
</div>

<script>
const api = () => window.pywebview.api;
const $ = (id) => document.getElementById(id);
let currentSession = 'default';
let currentModel = '';
let perms = { shell: 'ask', fs: 'allow' };
const PERM_TEXT = { ask: '需确认', allow: '允许', deny: '禁止' };

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function md(text) {
  let html = esc(text);
  // 先把 code 段替换成占位符，避免其内容被后续粗体规则波及（P2 #6）
  const codes = [];
  html = html.replace(/`([^`\n]+)`/g, (m, c) => {
    codes.push(c);
    return '\u0000' + (codes.length - 1) + '\u0000';
  });
  // 先匹配 ***x***（三级星号），再匹配 **x**，避免 ***粗体*** 被错切
  html = html.replace(/\*\*\*([^*\n]+)\*\*\*/g, '<span class="bold">$1</span>');
  html = html.replace(/\*\*([^*\n]+)\*\*/g, '<span class="bold">$1</span>');
  html = html.replace(/\u0000(\d+)\u0000/g, (m, i) => '<code>' + codes[Number(i)] + '</code>');
  return html;
}
function fmtTokens(n) {
  n = Number(n) || 0;
  if (n >= 10000) return (n / 10000).toFixed(1) + '万';
  return String(n);
}
function relTime(epoch) {
  if (!epoch) return '';
  const diff = (Date.now() / 1000 - epoch) / 60;
  if (diff < 1) return '刚刚';
  if (diff < 60) return Math.floor(diff) + '分';
  if (diff < 1440) return Math.floor(diff / 60) + '小时';
  return Math.floor(diff / 1440) + '天';
}

/* ---------- 通用对话框（JS Promise 驱动；Python 确认也复用它） ---------- */
window.__dialogResult = null;
function showDialog(opts) {
  $('dlgTitle').textContent = opts.title || '';
  $('dlgMsg').textContent = opts.message || '';
  $('dlgMsg').style.display = opts.message ? '' : 'none';
  const input = $('dlgInput');
  input.style.display = opts.input ? '' : 'none';
  input.value = opts.value || '';
  $('dlgOk').textContent = opts.confirmText || '确定';
  $('dlgCancel').textContent = opts.cancelText || '取消';
  $('dlgOverlay').style.display = 'flex';
  if (opts.input) { input.focus(); input.select(); }
}
function _closeDialog(value) {
  $('dlgOverlay').style.display = 'none';
  window.__dialogResult = value;
}
$('dlgOk').onclick = () => _closeDialog($('dlgInput').style.display !== 'none' ? $('dlgInput').value : true);
$('dlgCancel').onclick = () => _closeDialog(null);
$('dlgInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); $('dlgOk').click(); }
});
function dialogPrompt(title, value) {
  showDialog({ title, input: true, value });
  return new Promise(resolve => {
    const timer = setInterval(() => {
      if (window.__dialogResult !== null) {
        clearInterval(timer);
        const v = window.__dialogResult;
        window.__dialogResult = null;
        resolve(v);
      }
    }, 100);
  });
}

/* ---------- 主题 ---------- */
let theme = 'system';
const THEME_ICON = { dark: '🌙', light: '☀️', system: '💻' };
const THEME_TEXT = { dark: '深色', light: '浅色', system: '跟随系统' };
function applyTheme(mode) {
  theme = mode;
  document.documentElement.dataset.theme = mode;
  $('themeBtn').textContent = THEME_ICON[mode] + ' 外观: ' + THEME_TEXT[mode];
}
async function cycleTheme() {
  const order = ['dark', 'light', 'system'];
  const next = order[(order.indexOf(theme) + 1) % order.length];
  applyTheme(next);
  await api().set_theme(next);
}

/* ---------- 消息渲染 ---------- */
function copyText(text, btn) {
  const done = () => { btn.textContent = '已复制'; setTimeout(() => btn.textContent = '复制', 1200); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else fallbackCopy(text, done);
}
function fallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); done(); } catch (e) {}
  ta.remove();
}

function add(text, cls, meta, reasoning) {
  $('hero').style.display = 'none';
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  if (cls === 'user') {
    div.textContent = text;
  } else {
    if (reasoning) {
      const rd = document.createElement('div');
      rd.className = 'reasoning';
      rd.innerHTML = '<div class="r-head">🧠 已深度思考 · 点击展开/折叠</div><div class="r-body"></div>';
      rd.querySelector('.r-body').textContent = reasoning;
      rd.querySelector('.r-head').onclick = () => rd.classList.toggle('open');
      div.appendChild(rd);
    }
    const body = document.createElement('div');
    body.innerHTML = md(text);
    div.appendChild(body);
  }
  if (meta && meta.length) {
    const m = document.createElement('div');
    m.className = 'meta';
    for (const part of meta) {
      const span = document.createElement('span');
      span.className = cls === 'bot' && !part.startsWith('模型') ? 'toolchip' : '';
      span.textContent = part;
      m.appendChild(span);
    }
    div.appendChild(m);
  }
  $('thread').appendChild(div);
  $('log').scrollTop = $('log').scrollHeight;
  return div;
}

function showHeroIfEmpty() {
  const empty = !$('thread').children.length;
  $('hero').style.display = empty ? 'flex' : 'none';
  $('wsBar').style.display = empty ? 'flex' : 'none';  // 新会话时才显示工作区选择（对齐 dsh）
}

/* ---------- 状态刷新 ---------- */
async function refreshStatus() {
  const s = await api().status();
  currentModel = s.model || '';
  perms = { shell: s.shell_permission || 'ask', fs: s.fs_permission || 'allow' };
  renderPerm();
  const sel = $('modelSel');
  sel.innerHTML = '';
  for (const name of (s.models || [])) {
    const opt = document.createElement('option');
    opt.value = name; opt.textContent = name;
    if (name === currentModel) opt.selected = true;
    sel.appendChild(opt);
  }
  const think = $('thinkSel');
  if (!think.options.length) {
    for (const [v, label] of [['off','思考: 关'],['low','思考: 低'],['medium','思考: 中'],['high','思考: 高']]) {
      const opt = document.createElement('option');
      opt.value = v; opt.textContent = label;
      think.appendChild(opt);
    }
  }
  think.value = s.thinking_level || 'off';
  if (s.theme) applyTheme(s.theme);
  $('statusHint').textContent = (s.models && s.models.length)
    ? '' : '尚未配置模型 —— 点「模型与插件」填入 API Key';
  $('wsCur').textContent = s.workspace_name || s.workspace || '选择工作区';
  $('wsCur').title = s.workspace || '';
  refreshUsage();
}

function renderPerm() {
  const b = $('permBadge');
  b.textContent = '⌘ 命令' + PERM_TEXT[perms.shell] + ' · 写入' + PERM_TEXT[perms.fs];
  b.className = 'badge ' + (perms.shell === 'allow' && perms.fs === 'allow' ? 'allow'
                   : (perms.shell === 'deny' || perms.fs === 'deny') ? 'deny' : '');
  document.querySelectorAll('#permMenu button').forEach(btn =>
    btn.classList.toggle('sel', perms[btn.dataset.kind] === btn.dataset.mode));
}

function togglePermMenu() { $('permMenu').classList.toggle('open'); $('ctxCard').classList.remove('open'); }

async function pickPerm(kind, mode) {
  $('permMenu').classList.remove('open');
  const res = await api().set_permission(kind, mode);
  if (res.ok) { perms[kind] = mode; renderPerm(); }
}

document.addEventListener('mousedown', (e) => {
  const menu = $('permMenu');
  if (menu && !menu.contains(e.target) && e.target.id !== 'permBadge') menu.classList.remove('open');
  const card = $('ctxCard');
  if (card && !card.contains(e.target) && e.target.id !== 'ctxBtn') card.classList.remove('open');
  const wsMenu = $('wsMenu');
  if (wsMenu && !wsMenu.contains(e.target) && e.target.id !== 'wsBtn'
      && !e.target.closest('#wsBtn')) wsMenu.classList.remove('open');
});

/* ---------- 上下文占用 ---------- */
async function toggleCtxCard() {
  const card = $('ctxCard');
  if (card.classList.contains('open')) { card.classList.remove('open'); return; }
  const s = await api().context_usage();
  if (!s.ok) { card.innerHTML = '<div class="hint">' + esc(s.error || '暂不可用') + '</div>'; }
  else {
    const rows = (s.breakdown || []).map(item =>
      '<div class="ctx-row"><span class="dot' + (item.percent ? '' : ' dim') + '"></span>' +
      '<span class="name">' + esc(item.name) + '</span>' +
      '<span class="pct">' + (item.percent || 0) + '%</span></div>').join('');
    card.innerHTML =
      '<h4>上下文容量 <small>' + fmtTokens(s.used) + '/' + fmtTokens(s.window) +
      ' (' + (s.percent || 0) + '%)</small></h4>' +
      '<div class="ctx-bar"><i style="width:' + Math.min(s.percent || 0, 100) + '%"></i></div>' +
      '<div class="ctx-rows">' + rows + '</div>' +
      '<div class="hint">按当前会话消息与工具定义估算，非精确值。</div>';
  }
  card.classList.add('open');
  $('permMenu').classList.remove('open');
}

/* ---------- Token 用量 ---------- */
async function refreshUsage() {
  const res = await api().usage();
  const u = res.usage || {};
  $('usageLine').textContent = (u.prompt_tokens || u.completion_tokens)
    ? '累计输入 ' + fmtTokens(u.prompt_tokens) + ' · 输出 ' + fmtTokens(u.completion_tokens) + ' tokens'
    : '';
}

/* ---------- 侧栏（项目 → 会话，点击分组头收纳/展开） ---------- */
let sidebar = { workspaces: [], sessions: [] };
const collapsedWs = new Set();

async function refreshSidebar() {
  const [wsRes, sRes] = await Promise.all([api().workspaces(), api().sessions()]);
  sidebar.workspaces = wsRes.workspaces || [];
  sidebar.sessions = sRes.sessions || [];
  const tree = $('wsTree');
  tree.innerHTML = '';
  const currentWs = wsRes.current || '';
  for (const ws of sidebar.workspaces) {
    const group = document.createElement('div');
    group.className = 'ws-group' + (collapsedWs.has(ws.id) ? ' collapsed' : '');
    const head = document.createElement('div');
    head.className = 'ws-head';
    head.title = '点击收纳/展开会话';
    head.innerHTML = '<span class="ws-caret">▾</span><span>📁</span>' +
      '<span class="ws-name">' + esc(ws.name) + '</span>' +
      '<button class="row-menu" title="重命名工作区">⋯</button>';
    head.onclick = (e) => {
      if (e.target.closest('.row-menu')) return;
      if (collapsedWs.has(ws.id)) collapsedWs.delete(ws.id);
      else collapsedWs.add(ws.id);
      group.classList.toggle('collapsed', collapsedWs.has(ws.id));
    };
    head.querySelector('.row-menu').onclick = async (e) => {
      e.stopPropagation();
      const name = await dialogPrompt('重命名工作区', ws.name);
      if (name !== null && name.trim()) {
        await api().rename_workspace(name.trim());
        refreshSidebar(); refreshStatus();
      }
    };
    group.appendChild(head);
    for (const sess of sidebar.sessions.filter(x => x.ws === ws.id)) {
      const row = document.createElement('div');
      row.className = 's-row' + (sess.id === currentSession ? ' active' : '');
      row.innerHTML = '<span class="s-name">' + esc(sess.name) + '</span>' +
        '<span class="s-time">' + relTime(sess.updated) + '</span>' +
        '<button class="row-menu" title="重命名会话">⋯</button>';
      row.onclick = () => selectSession(sess.id);
      row.querySelector('.row-menu').onclick = async (e) => {
        e.stopPropagation();
        const name = await dialogPrompt('重命名会话', sess.name);
        if (name !== null && name.trim()) {
          await api().rename_session(sess.id, name.trim());
          refreshSidebar();
        }
      };
      group.appendChild(row);
    }
    if (!group.querySelector('.s-row')) {
      const empty = document.createElement('div');
      empty.className = 's-row';
      empty.style.opacity = '.45';
      empty.innerHTML = '<span class="s-name">（暂无会话）</span>';
      group.appendChild(empty);
    }
    tree.appendChild(group);
  }
  if (!sidebar.workspaces.length) {
    tree.innerHTML = '<div class="hint" style="padding:6px 10px">选择工作区后，会话会按项目分组显示。</div>';
  }
}

async function selectSession(id) {
  const s = await api().session_history(id);
  currentSession = id;
  $('thread').innerHTML = '';
  for (const m of (s.history || [])) add(m.content, m.role === 'user' ? 'user' : 'bot');
  showHeroIfEmpty();
  refreshSidebar();
}

async function newSessionFlow() {
  const name = await dialogPrompt('新会话名称', '');
  if (name === null) return;
  const s = await api().new_session();
  if (name.trim()) await api().rename_session(s.session, name.trim());
  currentSession = s.session;
  $('thread').innerHTML = '';
  showHeroIfEmpty();
  refreshSidebar();
}

/* ---------- 实时步骤（模型运作过程，对齐 dsh） ---------- */
let liveBlock = null;
let liveLlmCount = 0;

function onAgentEvent(evt) {
  if (!liveBlock) return;
  const steps = liveBlock.querySelector('.steps');
  if (evt.kind === 'llm') {
    liveLlmCount++;
    // 思考条目：带 reasoning 摘要，悬停可看全文
    const div = document.createElement('div');
    div.className = 'step pending';
    const reasoning = (evt.reasoning || '').trim();
    div.innerHTML = '<span class="s-icon">🧠</span><span class="s-text">思考 · 第 ' +
      liveLlmCount + ' 轮' + (evt.tools && evt.tools.length ? ' · 准备调用 ' + evt.tools.join(', ') : '') + '</span>';
    if (reasoning) div.title = reasoning;
    steps.appendChild(div);
  } else if (evt.kind === 'tool') {
    const firstLine = (evt.result || '').split('\n')[0].slice(0, 80);
    const div = document.createElement('div');
    div.className = 'step';
    div.innerHTML = '<span class="s-icon">🔧</span><span class="s-text">' +
      esc(evt.tool) + '</span><span class="s-time">' + ((evt.elapsed_ms || 0) / 1000).toFixed(1) + 's</span>';
    if (firstLine) {
      const res = document.createElement('div');
      res.className = 'step result';
      res.textContent = '↳ ' + firstLine;
      steps.appendChild(div);
      steps.appendChild(res);
    } else steps.appendChild(div);
  }
  const log = $('log');
  log.scrollTop = log.scrollHeight;
}

function showLiveBlock() {
  removeLiveBlock();
  liveLlmCount = 0;
  const div = document.createElement('div');
  div.className = 'msg bot';
  div.innerHTML = '<div class="steps"></div><span class="thinking"><i></i><i></i><i></i></span>';
  $('thread').appendChild(div);
  liveBlock = div;
  $('log').scrollTop = $('log').scrollHeight;
}
function removeLiveBlock() {
  if (liveBlock) { liveBlock.remove(); liveBlock = null; }
}

/* ---------- 发送 ---------- */
function setBusy(busy) {
  const send = $('send');
  send.classList.toggle('busy', busy);
  send.disabled = busy;
}

async function send() {
  const input = $('input');
  const text = input.value.trim();
  if (!text || $('send').disabled) return;
  input.value = ''; input.style.height = 'auto';
  add(text, 'user');
  showLiveBlock();
  setBusy(true);
  const res = await api().chat(text, currentSession);
  removeLiveBlock();
  setBusy(false);
  $('input').focus();
  if (!res.ok) { add(res.error || '未知错误', 'bot error'); return; }
  const meta = ['模型: ' + (res.model || '?')];
  for (const call of (res.tool_calls || [])) meta.push(call.name);
  add(res.reply || '(空回复)', 'bot', meta, res.reasoning || '');
  currentSession = res.session || currentSession;
  refreshSidebar();
  refreshStatus();
}

$('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
$('input').addEventListener('input', function () {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 180) + 'px';
});
$('modelSel').onchange = async () => {
  const res = await api().switch_model($('modelSel').value);
  if (res.ok) { currentModel = res.model; add(res.message, 'bot', ['模型: ' + res.model]); }
  else add(res.error || res.message, 'bot error');
  refreshStatus();
};
$('thinkSel').onchange = async () => {
  const res = await api().set_thinking($('thinkSel').value);
  if (res.ok) add(res.thinking_level === 'off' ? '已关闭思考' : '思考级别: ' + res.thinking_level, 'bot', ['思考']);
};

/* ---------- 设置弹窗 ---------- */
let editing = { models: [], default_model: '' };

function openSettings() {
  $('overlay').style.display = 'flex';
  switchTab('models');
  loadModels();
  loadPlugins();
}
function closeSettings() { $('overlay').style.display = 'none'; }
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $('tab-models').style.display = name === 'models' ? '' : 'none';
  $('tab-plugins').style.display = name === 'plugins' ? '' : 'none';
  $('tab-skills').style.display = name === 'skills' ? '' : 'none';
  $('tab-market').style.display = name === 'market' ? '' : 'none';
  if (name === 'market' && !marketLoaded) loadMarket();
  if (name === 'skills') loadSkills();
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeSettings(); $('dlgOverlay').style.display = 'none'; }
});
$('overlay').addEventListener('mousedown', (e) => { if (e.target === $('overlay')) closeSettings(); });

async function loadModels() {
  const s = await api().get_models();
  editing = { models: s.models || [], default_model: s.default_model || '' };
  renderCards();
  $('saveNote').textContent = '';
}

function renderCards() {
  const box = $('cards');
  box.innerHTML = '';
  editing.models.forEach((m, i) => {
    const card = document.createElement('div');
    card.className = 'model-card';
    card.innerHTML = `
      <div class="model-grid">
        <div class="field"><label>名称</label>
          <input value="${esc(m.name)}" oninput="editing.models[${i}].name=this.value"></div>
        <div class="field"><label>PROVIDER</label>
          <select onchange="editing.models[${i}].provider=this.value">
            <option value="openai"${m.provider !== 'anthropic' ? ' selected' : ''}>openai</option>
            <option value="anthropic"${m.provider === 'anthropic' ? ' selected' : ''}>anthropic</option>
          </select></div>
        <div class="field"><label>BASE URL（可选）</label>
          <input value="${esc(m.base_url || '')}" oninput="editing.models[${i}].base_url=this.value"></div>
      </div>
      <div class="model-grid2">
        <div class="field"><label>模型 ID</label>
          <input value="${esc(m.model || '')}" oninput="editing.models[${i}].model=this.value"></div>
        <div class="field"><label>API KEY</label>
          <input type="password" value="${esc(m.api_key || '')}" oninput="editing.models[${i}].api_key=this.value"></div>
        <label class="radio-default"><input type="radio" name="defmodel" data-idx="${i}"
          ${m.name === editing.default_model ? 'checked' : ''}>默认</label>
        <button class="icon-btn" onclick="editing.models.splice(${i},1);renderCards()">移除</button>
      </div>`;
    // 用事件监听而非内联 onchange='...('${name}')'：模型名含单引号会截断 JS 字符串（P2 #5）
    const radio = card.querySelector('input[type=radio]');
    radio.addEventListener('change', () => { editing.default_model = editing.models[i].name; });
    box.appendChild(card);
  });
}

function addCard() {
  editing.models.push({ name: '', provider: 'openai', base_url: '', model: '', api_key: '' });
  renderCards();
}

async function saveSettings() {
  const res = await api().save_models(editing.models, editing.default_model);
  const note = $('saveNote');
  note.className = res.ok ? 'note-ok' : 'note-err';
  note.textContent = res.ok ? (res.rebuild_error || '已保存，立即生效') : res.error;
  if (res.ok) refreshStatus();
}

/* ---------- 插件 ---------- */
async function loadPlugins() {
  const s = await api().plugins();
  const box = $('pluginCards');
  box.innerHTML = '';
  for (const p of (s.plugins || [])) {
    const row = document.createElement('div');
    row.className = 'plugin-row';
    const provided = Object.entries(p.provided || {})
      .map(([k, v]) => k + ': ' + v.join(', ')).join(' · ');
    row.innerHTML = `
      <span class="pname">${esc(p.name)}</span>
      <span class="plugin-state ${esc(p.state)}">${esc(p.state)}</span>
      <span class="plugin-provided" title="${esc(provided)}">${esc(provided)}</span>
      ${p.removable ? '<button class="icon-btn">移除</button>' : ''}`;
    const rmBtn = row.querySelector('button');
    if (rmBtn) rmBtn.onclick = async () => {
      if (!confirm('移除插件 ' + p.name + '？')) return;
      const r = await api().remove_plugin(p.name);
      if (!r.ok) { $('pluginNote').className = 'note-err'; $('pluginNote').textContent = r.error || '移除失败'; }
      loadPlugins();
    };
    box.appendChild(row);
  }
}

async function installPlugin() {
  const path = $('pluginPath').value.trim();
  if (!path) return;
  const res = await api().install_plugin(path);
  const note = $('pluginNote');
  note.className = res.ok ? 'note-ok' : 'note-err';
  note.textContent = res.ok ? (res.name + ' ' + (res.note || '')) : res.error;
  if (res.ok) { $('pluginPath').value = ''; loadPlugins(); }
}

/* ---------- 工作区（composer 上方下拉，新会话时选择） ---------- */
async function renderWsMenu() {
  const res = await api().workspaces();
  const list = res.workspaces || [];
  const menu = $('wsMenu');
  menu.innerHTML = '<div class="ws-menu-label">工作区</div>';
  for (const ws of list) {
    const btn = document.createElement('button');
    btn.className = res.current === ws.id ? 'sel' : '';
    btn.innerHTML = '<span>' + (res.current === ws.id ? '✓' : '📁') + '</span>' +
      '<span>' + esc(ws.name) + '</span><span class="ws-path">' + esc(ws.path) + '</span>';
    btn.onclick = async () => {
      menu.classList.remove('open');
      const r = await api().set_workspace(ws.path);
      if (r.ok) { refreshStatus(); refreshSidebar(); }
    };
    menu.appendChild(btn);
  }
  const browse = document.createElement('button');
  browse.innerHTML = '<span>📂</span><span>选择其他文件夹…</span>';
  browse.onclick = async () => { menu.classList.remove('open'); chooseWorkspace(); };
  menu.appendChild(browse);
}
function toggleWsMenu(e) {
  e.stopPropagation();
  const menu = $('wsMenu');
  if (!menu.classList.contains('open')) renderWsMenu();
  menu.classList.toggle('open');
}

async function chooseWorkspace() {
  const res = await api().choose_workspace();
  if (res.ok) {
    refreshStatus(); refreshSidebar();   // 切换工作区不打扰会话区
  } else if (res.error) {
    add(res.error, 'bot error');
  }
}

/* ---------- 插件市场 ---------- */
let marketLoaded = false;
let marketItems = [];

async function loadMarket(force) {
  const box = $('marketList');
  $('marketNote').textContent = '';
  box.innerHTML = '<div class="hint">正在加载插件市场…（首次需联网拉取目录）</div>';
  const res = await api().market_list(!!force);
  if (!res.ok) {
    box.innerHTML = '<div class="hint">加载失败: ' + esc(res.error || '网络不可用') +
      '<br>请检查网络后点「刷新」重试。</div>';
    return;
  }
  marketItems = res.items || [];
  marketLoaded = true;
  const cat = $('marketCat');
  const prev = cat.value;
  cat.innerHTML = '<option>全部</option>' +
    (res.categories || []).map(c => '<option>' + esc(c) + '</option>').join('');
  if (prev && [...cat.options].some(o => o.value === prev)) cat.value = prev;
  renderMarket();
}

function renderMarket() {
  const box = $('marketList');
  const query = ($('marketQuery').value || '').trim().toLowerCase();
  const cat = $('marketCat').value;
  const filtered = marketItems.filter(item =>
    (cat === '全部' || item.category === cat) &&
    (!query || (item.name + ' ' + item.desc).toLowerCase().includes(query)));
  box.innerHTML = '';
  if (!filtered.length) {
    box.innerHTML = '<div class="hint">没有匹配的插件。</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const item of filtered) {
    const row = document.createElement('div');
    row.className = 'market-row';
    row.innerHTML =
      '<div class="m-main"><div class="m-name"><a href="#" title="打开仓库">' + esc(item.name) + '</a></div>' +
      '<div class="m-desc">' + esc(item.desc) + '</div></div>' +
      '<span class="m-cat">' + esc(item.category) + '</span>' +
      '<span class="m-install"><button>安装</button></span>';
    row.querySelector('a').onclick = (e) => {
      e.preventDefault();
      api().open_url(item.url);  // 调用 Python 侧打开浏览器
    };
    const btn = row.querySelector('button');
    btn.onclick = () => marketInstall(item, btn);
    frag.appendChild(row);
  }
  box.appendChild(frag);
}

async function marketInstall(item, btn) {
  btn.disabled = true; btn.textContent = '安装中…';
  const res = await api().market_install(item.url);
  const note = $('marketNote');
  note.className = res.ok ? 'note-ok' : 'note-err';
  note.textContent = res.ok ? (item.name + ' 已安装 · ' + (res.note || '')) : (item.name + ': ' + res.error);
  btn.textContent = res.ok ? '已安装' : '安装';
  btn.disabled = res.ok;
}

/* ---------- 技能管理 ---------- */
async function loadSkills() {
  const res = await api().skills();
  const box = $('skillCards');
  box.innerHTML = '';
  if (!res.ok) { box.innerHTML = '<div class="hint">' + esc(res.error || '加载失败') + '</div>'; return; }
  if (!(res.skills || []).length) {
    box.innerHTML = '<div class="hint">暂无技能。插件可自带技能，也可以在下方安装本地 SKILL.md 目录。</div>';
  }
  for (const s of (res.skills || [])) {
    const row = document.createElement('div');
    row.className = 'plugin-row';
    row.innerHTML = '<span class="pname">' + esc(s.name) + '</span>' +
      '<span class="plugin-state ' + (s.source === 'profile' ? 'ACTIVE' : 'FAILED') + '">' +
      esc(s.source === 'profile' ? 'profile' : '插件') + '</span>' +
      '<span class="plugin-provided" title="' + esc(s.desc) + '">' + esc(s.desc) + '</span>' +
      (s.source === 'profile' ? '<button class="icon-btn">移除</button>' : '');
    const btn = row.querySelector('button');
    if (btn) btn.onclick = async () => {
      const r = await api().remove_skill(s.name);
      if (r.ok) loadSkills();
      else { $('skillNote').className = 'note-err'; $('skillNote').textContent = r.error; }
    };
    box.appendChild(row);
  }
}

async function installSkill() {
  const path = $('skillPath').value.trim();
  if (!path) return;
  const res = await api().install_skill(path);
  const note = $('skillNote');
  note.className = res.ok ? 'note-ok' : 'note-err';
  note.textContent = res.ok ? ('已安装技能: ' + res.name + '（新会话生效）') : res.error;
  if (res.ok) { $('skillPath').value = ''; loadSkills(); }
}

/* ---------- 右侧面板 ---------- */
let wsPath = '';

function togglePanel() {
  document.body.classList.toggle('panel-hidden');
  $('panelToggle').classList.toggle('off', document.body.classList.contains('panel-hidden'));
}
function switchPanel(name) {
  document.querySelectorAll('.rp-tab').forEach(t => t.classList.toggle('active', t.dataset.rp === name));
  document.querySelectorAll('.rp-body').forEach(b => b.classList.toggle('active', b.id === 'rp-' + name));
  if (name === 'ws') loadWsTree();
  if (name === 'review') loadReview();
}

async function loadWsTree() {
  const res = await api().ws_tree(wsPath);
  const pane = $('wsTreePane');
  if (!res.ok) {
    $('wsCrumb').textContent = '/';
    pane.innerHTML = '<div class="hint">' + esc(res.error || '无法读取') + '</div>';
    return;
  }
  wsPath = res.rel || '';
  $('wsCrumb').textContent = wsPath ? ('📁 ' + wsPath) : '📁 /（工作区根目录）';
  pane.innerHTML = '';
  if (!(res.entries || []).length) {
    pane.innerHTML = '<div class="hint">（空目录）</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const e of res.entries) {
    const row = document.createElement('div');
    row.className = 'tree-row';
    row.innerHTML = '<span>' + (e.dir ? '📁' : '📄') + '</span>' +
      '<span class="t-name">' + esc(e.name) + '</span>' +
      (e.dir ? '' : '<span class="t-size">' + fmtTokens(e.size) + 'B</span>');
    if (e.dir) row.onclick = () => { wsPath = wsPath ? wsPath + '/' + e.name : e.name; loadWsTree(); };
    frag.appendChild(row);
  }
  pane.appendChild(frag);
}
function wsUp() {
  if (!wsPath) return;
  const idx = wsPath.lastIndexOf('/');
  wsPath = idx === -1 ? '' : wsPath.slice(0, idx);
  loadWsTree();
}

async function termRun() {
  const input = $('termInput');
  const cmd = input.value.trim();
  if (!cmd) return;
  input.value = '';
  const out = $('termOut');
  out.textContent += '\n$ ' + cmd + '\n';
  out.scrollTop = out.scrollHeight;
  const res = await api().run_terminal(cmd);
  out.textContent += (res.ok ? res.output : res.error) + '\n';
  out.scrollTop = out.scrollHeight;
}

function brGo() {
  let url = $('brUrl').value.trim();
  if (!url) return;
  if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
  $('brUrl').value = url;
  $('browserFrame').src = url;
}
function brExternal() {
  const url = $('brUrl').value.trim();
  if (url) api().open_url(/^https?:\/\//i.test(url) ? url : 'https://' + url);
}

async function loadReview() {
  const out = $('reviewOut');
  out.textContent = '正在生成…';
  const res = await api().review();
  if (!res.ok) { out.textContent = res.error || '无法生成'; return; }
  out.innerHTML = '';
  for (const line of res.text.split('\n')) {
    const span = document.createElement('span');
    if (line.startsWith('+') && !line.startsWith('+++')) span.className = 'diff-add';
    else if (line.startsWith('-') && !line.startsWith('---')) span.className = 'diff-del';
    span.textContent = line + '\n';
    out.appendChild(span);
  }
}

/* ---------- 辅助对话（独立会话） ---------- */
const AUX_SESSION = 'aux';
let auxBusy = false;

async function auxSend() {
  const input = $('auxInput');
  const text = input.value.trim();
  if (!text || auxBusy) return;
  input.value = '';
  auxBusy = true;
  $('auxSend').classList.add('busy');
  const busy = document.createElement('div');
  busy.className = 'msg bot';
  busy.innerHTML = '<span class="thinking"><i></i><i></i><i></i></span>';
  $('auxThread').appendChild(busy);
  $('auxThread').scrollTop = $('auxThread').scrollHeight;
  const res = await api().chat(text, AUX_SESSION);
  busy.remove();
  auxBusy = false;
  $('auxSend').classList.remove('busy');
  if (!res.ok) { auxAdd(res.error || '未知错误', 'bot error'); return; }
  auxAdd(text, 'user');
  auxAdd(res.reply || '(空回复)', 'bot', res.reasoning || '');
  $('auxThread').scrollTop = $('auxThread').scrollHeight;
}
function auxAdd(text, cls, reasoning) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  if (cls === 'user') div.textContent = text;
  else {
    if (reasoning) {
      const rd = document.createElement('div');
      rd.className = 'reasoning';
      rd.innerHTML = '<div class="r-head">🧠 思考 · 点击展开</div><div class="r-body"></div>';
      rd.querySelector('.r-body').textContent = reasoning;
      rd.querySelector('.r-head').onclick = () => rd.classList.toggle('open');
      div.appendChild(rd);
    }
    const body = document.createElement('div');
    body.innerHTML = md(text);
    div.appendChild(body);
  }
  $('auxThread').appendChild(div);
}
async function auxInit() {
  const res = await api().session_history(AUX_SESSION);
  for (const m of (res.history || [])) auxAdd(m.content, m.role === 'user' ? 'user' : 'bot');
  $('auxThread').scrollTop = $('auxThread').scrollHeight;
  await api().rename_session(AUX_SESSION, '辅助对话');
  refreshSidebar();
}
$('auxInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); auxSend(); }
});
$('termInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); termRun(); }
});
$('brUrl').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); brGo(); }
});

/* ---------- 启动 ---------- */
window.addEventListener('pywebviewready', async () => {
  await refreshStatus();
  await refreshSidebar();
  const s = await api().session_history(currentSession);
  for (const m of (s.history || [])) add(m.content, m.role === 'user' ? 'user' : 'bot');
  showHeroIfEmpty();
  await auxInit();
  $('input').focus();
});
</script></body></html>"""


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run(host, title: str = "卅 harness", width: int = 1180, height: int = 780) -> int:
    """创建原生窗口并进入事件循环（阻塞到窗口关闭）。"""
    try:
        import webview  # pywebview
    except ImportError:
        # from None：这里的 ImportError 只是「没装界面库」，链上去只会让人以为
        # 是 pywebview 内部出了问题；真正要读的是下面这段安装指引。
        raise SystemExit(
            "桌面端需要 pywebview：\n"
            "    pip install pywebview\n"
            "或安装时带上桌面扩展：pip install sahou-harness[desktop]"
        ) from None
    app = DesktopApp(host)
    # 沿用上次在界面里选择的工作区（优先于 CLI --workspace）
    try:
        config = host.profile.load_config()
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    saved_ws = (config.get("workspace") or "").strip()
    if saved_ws and Path(saved_ws).is_dir():
        host.workspace = str(Path(saved_ws).resolve())
    # 兜底：无论来自何处，进入界面时都保证工作区是绝对路径
    host.workspace = str(Path(host.workspace or ".").resolve())
    # 首帧就带正确主题，避免加载后闪一次颜色；默认浅色
    theme = (config.get("theme") or "").strip()
    if theme not in THEME_MODES:
        theme = "light"
    html = UI_HTML.replace('<html lang="zh">', f'<html lang="zh" data-theme="{theme}">', 1)
    window = webview.create_window(
        title, html=html, js_api=app,
        background_color="#f7f7f8" if theme == "light" else "#0f0f0f",
        width=width, height=height, min_size=(940, 620))
    app._window = window
    webview.start()  # Windows 上使用 WebView2 原生运行时
    return 0
