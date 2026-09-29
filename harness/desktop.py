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
from typing import Any, ClassVar
from urllib.parse import urlparse

from .config import PERMISSION_MODES, ConfigError, permission_mode
from .kernel import ACTIVE
from .procutil import CREATE_NO_WINDOW

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
        self._busy_sessions: set = set()  # 正在回答的会话（允许多会话并行）
        self._cancel_sessions: set = set()  # 请求中断流式回复的会话（stop_stream 置位）
        self._market_cache = None       # (时间戳, items, categories)，10 分钟 TTL
        self._market_lock = threading.Lock()
        self._approvals: dict[str, tuple[threading.Event, dict]] = {}  # 待确认请求
        self._approval_lock = threading.Lock()
        host.confirm = self._confirm  # 兼容旧通道：单一「允许/拒绝」
        # 新版审批通道：允许一次 / 一直允许 / 拒绝（写入类还带 diff）
        host.request_approval = self._request_approval

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
        # 默认名不用裸 id（s-2026...）：兜底成可读的「会话 月-日 时:分」
        info.setdefault("name", "会话 " + time.strftime("%m-%d %H:%M"))
        info["ws"] = info.get("ws") or self._current_ws(config)
        info["updated"] = time.time()
        meta[session_id] = info
        self.host.profile.update_config(sessions_meta=meta)

    def _maybe_auto_title(self, session_id: str, user_text: str, reply_text: str) -> None:
        """首轮回复后给「还没命名」的会话自动起标题（config.auto_title=false 关闭）。

        只覆盖默认名（「会话 月-日 时:分」或空）——用户或新会话向导起过的名字不动。
        标题用当前模型一次廉价调用生成；任何失败都静默保留默认名（起名是锦上添花，
        不能影响对话主链路）。
        """
        try:
            config = self._config()
            if config.get("auto_title") is False:
                return
            meta = config.get("sessions_meta") or {}
            name = str((meta.get(session_id) or {}).get("name") or "")
            if name and not name.startswith("会话 "):
                return
            runtime = self.host.service("models_runtime") or {}
            pool = runtime.get("pool") or {}
            llm = pool.get(runtime.get("current") or "")
            if llm is None or not callable(getattr(llm, "chat", None)):
                return
            prompt = ("为下面的对话起一个不超过12个字的中文标题。"
                      "只输出标题本身：不要引号、句号、前缀或任何解释。\n\n"
                      f"用户：{str(user_text or '')[:300]}\n"
                      f"助手：{str(reply_text or '')[:300]}")
            response = llm.chat([{"role": "user", "content": prompt}])
            lines = str(getattr(response, "content", "") or "").strip().splitlines()
            title = lines[0].strip().strip("「」\"'《》。．.。")[:24] if lines else ""
            if title:
                self.rename_session(session_id, title)
        except Exception:  # noqa: BLE001 —— 起名失败不影响对话
            pass

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
                # 读后立即清空（H-08c）：终端命令与对话工具共用这一个结果变量，
                # 不清空的话并发等待者会把同一次点击读走（结果串扰/误放行）。
                try:
                    self._window.evaluate_js("window.__dialogResult=null")
                except Exception:  # noqa: BLE001 —— 窗口刚关不影响已取得的结果
                    pass
                return bool(value)
            time.sleep(interval)
            interval = min(interval * 1.5, 1.0)
        return False

    # -- 审批通道（allow_once / allow_always / deny） ------------------------
    def _request_approval(self, payload: dict) -> str:
        """工具线程发起的人工确认：弹窗 → 等用户选择（超时或窗口关闭即拒绝）。"""
        if self._window is None:
            return "deny"
        req_id = hashlib.md5(
            f"{time.time()}-{threading.get_ident()}".encode()).hexdigest()[:10]
        event = threading.Event()
        slot: dict = {"decision": "deny"}
        with self._approval_lock:
            self._approvals[req_id] = (event, slot)
        try:
            self._push_event({"kind": "approval", "id": req_id,
                              **{k: v for k, v in payload.items() if k != "kind"},
                              "approval_kind": payload.get("kind") or "shell"})
            timeout = CONFIRM_TIMEOUT
            try:
                cfg = self._config().get("permissions")
                if isinstance(cfg, dict) and cfg.get("approval_timeout"):
                    timeout = float(cfg["approval_timeout"])
            except Exception:  # noqa: BLE001 —— 配置读失败用默认超时
                timeout = CONFIRM_TIMEOUT
            if not event.wait(max(5.0, timeout)):
                return "deny"
            return str(slot.get("decision") or "deny")
        finally:
            with self._approval_lock:
                self._approvals.pop(req_id, None)

    def resolve_approval(self, req_id: str, decision: str) -> dict[str, Any]:
        """界面回填审批结果（js_api）。"""
        with self._approval_lock:
            entry = self._approvals.get(str(req_id or ""))
        if entry is None:
            return {"ok": False, "error": "该确认请求已失效"}
        event, slot = entry
        slot["decision"] = decision if decision in ("allow_once", "allow_always", "deny") \
            else "deny"
        event.set()
        return {"ok": True, "decision": slot["decision"]}

    # -- 文件改动检查点 -----------------------------------------------------
    def checkpoints(self, limit: int = 20) -> dict[str, Any]:
        """列出文件改动检查点（最新在前）。"""
        from harness.checkpoints import list_checkpoints

        return {"ok": True, "items": list_checkpoints(self.host.profile, limit=limit)}

    def undo_checkpoint(self, cid: str = "") -> dict[str, Any]:
        """回滚最近一次（或指定）检查点。"""
        from harness.checkpoints import undo

        result = undo(self.host.profile, cid or None)
        if result.get("ok"):
            result["workspace"] = str(getattr(self.host, "workspace", "") or "")
        return result

    def set_confirm_write(self, enabled: bool) -> dict[str, Any]:
        """开关「写文件前显示 diff 确认」（permissions.confirm_write）。"""
        config = self._config()
        permissions = dict(config.get("permissions") or {})
        permissions["confirm_write"] = bool(enabled)
        self.host.profile.update_config(permissions=permissions)
        return {"ok": True, "confirm_write": bool(enabled)}

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
            "session_name": self._display_name(
                self._current_session, meta),
            "shell_permission": permission_mode(config, "shell"),
            "fs_permission": permission_mode(config, "fs"),
            "git_permission": permission_mode(config, "git"),
            "confirm_write": bool((config.get("permissions") or {}).get("confirm_write")
                                  if isinstance(config.get("permissions"), dict) else False),
            "thinking_level": config.get("thinking_level") or "off",
            "plan_mode": bool(config.get("plan_mode")),
            "theme": config.get("theme") if config.get("theme") in THEME_MODES else "light",
            "context_window": int(getattr(llm, "context_window", 0) or 0),
            "usage": usage,
        }

    def set_permission(self, kind: str, mode: str) -> dict[str, Any]:
        if kind not in ("shell", "fs", "git"):
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

    def pick_image(self) -> dict[str, Any]:
        """弹出原生文件对话框选图片（可多选），返回绝对路径列表（功能7）。"""
        if self._window is None:
            return {"ok": False, "error": "窗口未就绪"}
        try:
            import webview

            picked = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=("图片 (*.png;*.jpg;*.jpeg;*.gif;*.webp)", "所有文件 (*.*)"))
        except Exception as exc:  # noqa: BLE001 —— 用户取消或对话框失败
            return {"ok": False, "error": str(exc)}
        if not picked:
            return {"ok": True, "paths": []}
        paths = picked if isinstance(picked, (list, tuple)) else [picked]
        return {"ok": True, "paths": [str(p) for p in paths]}

    def check_image(self, path: str) -> dict[str, Any]:
        """校验图片路径（工作区路径监狱 + 扩展名），通过才允许附加（功能7）。"""
        from harness.workspace import safe_image

        try:
            return {"ok": True, "path": safe_image(self.host, path)}
        except (ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)}

    def image_preview(self, path: str) -> dict[str, Any]:
        """把工作区内图片读成 data URL 供界面点击预览（路径监狱内，≤15MB）。"""
        import base64

        from harness.workspace import safe_image

        try:
            p = Path(safe_image(self.host, path))
            if p.stat().st_size > 15 * 1024 * 1024:
                return {"ok": False, "error": "图片超过 15MB，无法预览"}
            data = base64.b64encode(p.read_bytes()).decode("ascii")
        except (ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}.get(p.suffix.lower(),
                                                                "image/png")
        return {"ok": True, "data": f"data:{mime};base64,{data}"}

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
        # off 也显式下发（llm 端转为 enable_thinking=false），不留给服务端默认
        for llm in (runtime.get("pool") or {}).values():
            llm.reasoning_effort = level
        return {"ok": True, "thinking_level": level}

    def set_plan_mode(self, on: bool) -> dict[str, Any]:
        """切换计划模式（📋 按钮）：写配置并重建 agent（记忆按会话回放不丢）。"""
        fn = self.host.service("set_plan_mode")
        if not callable(fn):
            return {"ok": False, "error": "对话插件未激活"}
        message = str(fn(bool(on)))
        return {"ok": True, "plan_mode": bool(on), "message": message}

    # -- 主题 -----------------------------------------------------------------
    def set_theme(self, mode: str) -> dict[str, Any]:
        if mode not in THEME_MODES:
            return {"ok": False, "error": f"未知主题: {mode}"}
        self.host.profile.update_config(theme=mode)
        return {"ok": True, "theme": mode}

    # -- 界面偏好（右侧面板宽度 / 工作区选择条显隐等） -------------------------
    def get_ui_prefs(self) -> dict[str, Any]:
        try:
            config = self.host.profile.load_config()
        except ConfigError:
            return {"ok": True, "prefs": {}}
        prefs = config.get("ui_prefs")
        return {"ok": True, "prefs": dict(prefs) if isinstance(prefs, dict) else {}}

    def set_ui_prefs(self, prefs: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(prefs, dict):
            return {"ok": False, "error": "prefs 必须是对象"}
        try:
            self.host.profile.update_config(ui_prefs=dict(prefs))
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

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
                creationflags=CREATE_NO_WINDOW,
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
    @staticmethod
    def _display_name(session_id: str, info: dict[str, Any]) -> str:
        """会话显示名：历史遗留的裸 id（s-2026...）兜底为可读名。"""
        name = str(info.get("name") or "").strip()
        if name and name != session_id:
            return name
        updated = info.get("updated") or 0
        if updated:
            return "会话 " + time.strftime("%m-%d %H:%M", time.localtime(updated))
        return "未命名会话"

    def sessions(self) -> dict[str, Any]:
        config = self._config()
        meta = config.get("sessions_meta") or {}
        ids = set(self.host.profile.session_ids()) | set(meta)  # 含已命名但尚无消息的会话
        items = []
        for session_id in ids:
            info = meta.get(session_id) or {}
            items.append({
                "id": session_id,
                "name": self._display_name(session_id, info),
                "ws": info.get("ws") or "",
                "updated": info.get("updated") or 0,
                "pinned": bool(info.get("pinned")),
                "done": bool(info.get("done")),
                "order": info.get("order", 10 ** 9),
            })
        # 置顶优先，已完成的排最后；拖拽排序（order 小在前），同序按最近更新
        items.sort(key=lambda item: (item["done"], not item["pinned"],
                                     item["order"], -item["updated"]))
        return {"ok": True, "sessions": items, "current": self._current_session}

    def mark_session_done(self, session_id: str, done: bool) -> dict[str, Any]:
        """标记会话完成 / 取消完成（F8：验收后销毁会话的纪律配套）。"""
        sid = str(session_id or "").strip()
        if not sid:
            return {"ok": False, "error": "缺少会话 id"}
        config = self._config()
        meta = dict(config.get("sessions_meta") or {})
        info = dict(meta.get(sid) or {})
        info["done"] = bool(done)
        meta[sid] = info
        self.host.profile.update_config(sessions_meta=meta)
        return {"ok": True, "session": sid, "done": bool(done)}

    def set_session_pin(self, session_id: str, pinned: bool) -> dict[str, Any]:
        """置顶 / 取消置顶会话（sessions_meta.pinned；界面排在分组最前）。"""
        sid = str(session_id or "").strip()
        if not sid:
            return {"ok": False, "error": "缺少会话 id"}
        config = self._config()
        meta = dict(config.get("sessions_meta") or {})
        info = dict(meta.get(sid) or {})
        info["pinned"] = bool(pinned)
        meta[sid] = info
        self.host.profile.update_config(sessions_meta=meta)
        return {"ok": True, "session": sid, "pinned": bool(pinned)}

    def set_session_order(self, ids: list) -> dict[str, Any]:
        """保存会话手动排序（侧栏拖拽）：按传入顺序写 sessions_meta.order。

        只更新列出的会话，未列出的保持原 order（同 order 按最近更新排序）。
        """
        meta = dict(self._config().get("sessions_meta") or {})
        for idx, raw in enumerate(ids or []):
            sid = str(raw or "").strip()
            if not sid or sid not in meta:
                continue
            info = dict(meta.get(sid) or {})
            info["order"] = idx
            meta[sid] = info
        self.host.profile.update_config(sessions_meta=meta)
        return {"ok": True, "count": len(ids or [])}

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

    def delete_session(self, session_id: str) -> dict[str, Any]:
        """删除会话（文件 + 命名元数据）；删的是当前会话时自动切到最近的一个。

        只有名字（sessions_meta）、还没有消息的会话也允许删除——
        「新会话」正是这种状态，否则界面上会出现删不掉的空会话。
        """
        sid = str(session_id or "").strip()
        if not sid:
            return {"ok": False, "error": "缺少会话 id"}
        file_removed = self.host.profile.delete_session(sid)
        config = self._config()
        meta = dict(config.get("sessions_meta") or {})
        had_meta = sid in meta
        if had_meta:
            del meta[sid]
            self.host.profile.update_config(sessions_meta=meta)
        if not file_removed and not had_meta:
            return {"ok": False, "error": f"会话不存在: {sid}"}
        next_id = ""
        if sid == self._current_session:
            sessions_dir = self.host.profile.sessions_dir
            ids = self.host.profile.session_ids()
            if ids:
                next_id = max(ids, key=lambda i: (
                    sessions_dir / f"{i}.json").stat().st_mtime)
                self._current_session = next_id
            else:
                created = self.new_session()
                next_id = str(created.get("session") or "")
        return {"ok": True, "deleted": sid, "next": next_id}

    # -- 会话搜索 / 重新生成 / 编辑重发 ----------------------------------------
    @staticmethod
    def _visible_history(history: list[dict]) -> list[tuple[int, dict]]:
        """[(真实下标, 消息)]：只含 user/assistant，与界面渲染顺序一致。"""
        return [(i, m) for i, m in enumerate(history)
                if m.get("role") in ("user", "assistant")]

    def search_sessions(self, query: str, scope: str = "current") -> dict[str, Any]:
        """按关键词搜会话消息。scope="current" 只搜当前会话，"all" 搜全部历史。

        返回命中项 [{session, role, index, snippet}]：index 是**过滤后**
        （只数 user/assistant）的消息序号，与界面上 .msg 的 data-hi 对应，
        前端可直接滚动定位。
        """
        q = str(query or "").strip().lower()
        if not q:
            return {"ok": False, "error": "关键词为空"}
        if scope == "current":
            ids = [self._current_session]
        elif scope == "all":
            ids = self.host.profile.session_ids()
        else:
            return {"ok": False, "error": f"未知搜索范围: {scope}"}
        meta = self._config().get("sessions_meta") or {}
        hits: list[dict[str, Any]] = []
        for sid in ids:
            visible = self._visible_history(self.host.profile.load_session(sid))
            for index, (_, msg) in enumerate(visible):
                content = str(msg.get("content") or "")
                pos = content.lower().find(q)
                if pos == -1:
                    continue
                start = max(0, pos - 40)
                snippet = ("…" if start else "") + content[start:pos + len(q) + 60] + "…"
                hits.append({
                    "session": sid,
                    "session_name": self._display_name(sid, meta.get(sid) or {}),
                    "role": str(msg.get("role")),
                    "index": index,
                    "snippet": snippet.replace("\n", " ")[:160],
                })
                if len(hits) >= 60:  # 上限防爆量；界面提示缩小关键词
                    return {"ok": True, "hits": hits, "truncated": True}
        return {"ok": True, "hits": hits, "truncated": False}

    def _truncate_and_rebind(self, session_id: str, keep_upto: int) -> bool:
        """把会话历史截断到 ``history[:keep_upto]``，并让 agent 按磁盘历史重建。

        重新生成 / 编辑重发的公共底座：截断必须同步丢掉内存里 agent 的旧历史
        （new_session 会把 chat_loop 的 agent 置空，下轮按截断后的文件回放），
        否则文件改了、上下文还是旧的。
        """
        history = self.host.profile.load_session(session_id)
        if keep_upto < 0 or keep_upto > len(history):
            return False
        self.host.profile.save_session(session_id, history[:keep_upto])
        reset = self.host.service("new_session")
        if callable(reset):
            reset(session_id)
        return True

    def regenerate_last(self) -> dict[str, Any]:
        """重新生成当前会话的最后一条回复。

        做法是截掉「最后一条用户消息及其后的全部内容」再原样重发 ——
        这样 ask 会把这条用户消息重新写回历史，语义与用户手动重发一致。
        原消息附带的图片不会保留（历史里只存文本），属已知限制。
        """
        sid = self._current_session
        history = self.host.profile.load_session(sid)
        visible = self._visible_history(history)
        last_user_real = next(
            (real for real, msg in reversed(visible) if msg.get("role") == "user"),
            None)
        if last_user_real is None:
            return {"ok": False, "error": "没有可重新生成的用户消息"}
        text = str(history[last_user_real].get("content") or "").strip()
        if not text:
            return {"ok": False, "error": "最后一条用户消息为空，无法重新生成"}
        if not self._truncate_and_rebind(sid, last_user_real):
            return {"ok": False, "error": "截断历史失败"}
        result = self.chat(text)
        result["regenerated"] = bool(result.get("ok"))
        return result

    def edit_message_resend(self, index: int, new_text: str) -> dict[str, Any]:
        """编辑某条**用户**消息并重新发送：截掉它及其后的内容，用新文本重跑。

        ``index`` 是过滤后（只数 user/assistant）的序号，与界面 data-hi 一致。
        """
        sid = self._current_session
        visible = self._visible_history(self.host.profile.load_session(sid))
        idx = int(index)
        if idx < 0 or idx >= len(visible):
            return {"ok": False, "error": f"消息序号越界: {idx}"}
        real, msg = visible[idx]
        if msg.get("role") != "user":
            return {"ok": False, "error": "只能编辑用户消息"}
        new_text = str(new_text or "").strip()
        if not new_text:
            return {"ok": False, "error": "新内容为空"}
        if not self._truncate_and_rebind(sid, real):
            return {"ok": False, "error": "截断历史失败"}
        result = self.chat(new_text)
        result["edited"] = bool(result.get("ok"))
        return result

    def _ensure_session_workspace(self, session_id: str) -> None:
        """会话 ↔ 工作区联动：继续某个会话时切回它首次使用的工作区。

        修复「打开旧会话后 agent 描述的是另一个工作区的文件」——会话历史
        属于某个工程，继续对话时工具与上下文必须指回那个工程；否则模型
        要么答错目录，要么凭回放历史里的旧清单作答。目录已不存在则不动。
        """
        sid = str(session_id or "").strip()
        if not sid:
            return
        config = self._config()
        ws_id = ((config.get("sessions_meta") or {}).get(sid) or {}).get("ws")
        if not ws_id or ws_id == self._current_ws(config):
            return
        entries = {e.get("id"): e for e in (config.get("workspaces") or [])}
        path = str((entries.get(ws_id) or {}).get("path") or "")
        if path and Path(path).is_dir():
            self.set_workspace(path)

    def chat(self, message: str, session_id: str = "", images: list | None = None,
             model: str = "") -> dict[str, Any]:
        session_id = session_id or self._current_session
        self._ensure_session_workspace(session_id)
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
                # images（功能7）：前端附加的图片源（http(s) URL 或本地路径），
                # 路径校验在 chat_loop.ask 内做（工作区路径监狱），越界回 ok=False。
                # model 非空且≠当前模型时走 ask_with（辅助对话选模型）。
                model_name = str(model or "").strip()
                ask_with = self.host.service("ask_with")
                if model_name and callable(ask_with):
                    result = ask_with(message, session_id=session_id,
                                      images=images or None, model_name=model_name)
                else:
                    result = ask(message, session_id=session_id, images=images or None)
            except Exception as exc:  # noqa: BLE001 —— 错误回到界面而不是崩窗口
                return {"ok": False, "error": str(exc)}
            finally:
                self.host.emit_agent_event = None
            result["ok"] = True
            result["session"] = session_id
            self._touch_session(session_id)
        # 锁外起名：标题生成的 LLM 调用可能要几秒，不能占着全局锁挡其他会话
        self._maybe_auto_title(session_id, message, str(result.get("reply") or ""))
        warn = self._usage_budget_warning()
        if warn:
            result["budget_warning"] = warn
        return result

    def can_stream(self) -> dict[str, Any]:
        """当前模型是否支持流式（前端据此选择流式或整段路径）。"""
        fn = self.host.service("can_stream")
        try:
            stream = bool(callable(fn) and fn())
        except Exception:  # noqa: BLE001 —— 模型未配置等
            stream = False
        return {"ok": True, "stream": stream}

    def compare_models(self, message: str, model_a: str = "", model_b: str = "") -> dict[str, Any]:
        """多模型并答对比：同一问题并行问池子里两个模型（纯对话、无工具、不落会话）。

        对比是「选模型」不是「干活」：临时 agent 内存里跑完即弃，不写会话文件、
        不记 usage.jsonl、不带工具（避免两边的工具副作用互相干扰计时与答案）。
        model_a 省略=当前模型；model_b 省略=池子里下一个不同的模型。
        """
        message = str(message or "").strip()
        if not message:
            return {"ok": False, "error": "消息不能为空"}
        runtime = self.host.service("models_runtime") or {}
        pool = runtime.get("pool") or {}
        build = self.host.service("build_agent_with")
        if not callable(build):
            return {"ok": False, "error": "对话插件未激活"}
        if not pool:
            return {"ok": False, "error": "没有可用模型"}
        name_a = str(model_a or "").strip() or str(runtime.get("current") or "")
        name_b = str(model_b or "").strip()
        if not name_b:
            order = runtime.get("order") or []
            name_b = next((n for n in order if n != name_a), "")
        if not name_b:
            return {"ok": False, "error": "池子里只有一个可用模型——对比至少需要两个不同的模型（先在设置里再加一个）"}
        for name in (name_a, name_b):
            if not name or name not in pool:
                return {"ok": False, "error": f"模型不存在或不可用: {name or '（空）'}"}
        if name_a == name_b:
            return {"ok": False, "error": "对比需要两个不同的模型（先在设置里再加一个）"}

        results: dict[str, dict[str, Any]] = {}

        def run_one(name: str) -> None:
            started = time.time()
            try:
                agent = build(pool[name], f"cmp-{name}", tools=[])
                result = agent.run(message, session_id=f"cmp-{name}")
                results[name] = {
                    "ok": True,
                    "reply": result.content or "",
                    "reasoning": getattr(result, "reasoning", "") or "",
                    "usage": getattr(result, "usage", {}) or {},
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            except Exception as exc:  # noqa: BLE001 —— 单边失败不拖垮另一边
                results[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                                 "elapsed_ms": int((time.time() - started) * 1000)}

        threads = [threading.Thread(target=run_one, args=(n,), name=f"sha-cmp-{n}")
                   for n in (name_a, name_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return {"ok": True, "message": message,
                "a": {"name": name_a, **results.get(name_a, {"ok": False, "error": "无结果"})},
                "b": {"name": name_b, **results.get(name_b, {"ok": False, "error": "无结果"})}}

    def branch_session(self, session_id: str, index: int) -> dict[str, Any]:
        """从第 index 条可见消息（0 起）分叉新会话：复制该条及之前的历史，原会话不动。

        index 用「可见消息序号」（与界面渲染一致：user/assistant 且内容非空），
        后端按同一过滤规则换算回原始下标，避免空消息造成的错位。
        """
        sid = str(session_id or "").strip() or self._current_session
        history = self.host.profile.load_session(sid)
        visible = [i for i, m in enumerate(history) if str(m.get("content") or "").strip()]
        try:
            pos = int(index)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"序号非法: {index}"}
        if pos < 0 or pos >= len(visible):
            return {"ok": False, "error": f"序号越界: {index}（共 {len(visible)} 条可见消息）"}
        base = time.strftime("s-%Y%m%d-%H%M%S")
        existing = set(self.host.profile.session_ids())
        new_id = base
        counter = 2
        while new_id in existing:
            new_id = f"{base}-{counter}"
            counter += 1
        self.host.profile.save_session(new_id, history[: visible[pos] + 1])
        meta = dict(self._config().get("sessions_meta") or {})
        info = dict(meta.get(sid) or {})
        name = str(info.get("name") or "会话")
        meta[new_id] = {"name": name + " ⎇", "ws": info.get("ws") or self._current_ws(),
                        "updated": time.time()}
        self.host.profile.update_config(sessions_meta=meta)
        return {"ok": True, "session": new_id, "messages": pos + 1}

    # -- 长期记忆 / 改动时间线 / 预算 / 备份恢复 -----------------------------------
    def agent_memory(self) -> dict[str, Any]:
        """长期记忆（profile/memory.md）：构建 agent 时注入 system prompt，此处读写。"""
        path = Path(self.host.profile.root) / "memory.md"
        try:
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "text": text, "path": str(path)}

    def save_agent_memory(self, text: str) -> dict[str, Any]:
        from .config import atomic_write_text

        path = Path(self.host.profile.root) / "memory.md"
        try:
            atomic_write_text(path, str(text or ""))
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        reset = self.host.service("reset_agent")
        if callable(reset):
            reset()   # 下一轮对话立刻带上新记忆
        return {"ok": True, "chars": len(str(text or ""))}

    def checkpoint_diff(self, cid: str, max_chars: int = 8000) -> dict[str, Any]:
        """改动时间线的 diff 预览：检查点里的旧内容 vs 磁盘当前内容（unified diff）。"""
        import difflib

        base = Path(self.host.profile.root) / "checkpoints" / str(cid or "")
        try:
            manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"检查点不可读: {exc}"}
        parts: list[str] = []
        for entry in manifest.get("files") or []:
            rel = str(entry.get("rel") or "")
            target = Path(entry.get("abs") or "")
            new = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
            if not entry.get("existed"):
                preview = "\n".join(new.splitlines()[:60])
                parts.append(f"### {rel}（当时新建）\n{preview or '（当时不存在）'}")
                continue
            backup = base / "files" / str(entry.get("backup") or "")
            old = backup.read_text(encoding="utf-8", errors="replace") if backup.is_file() else ""
            diff = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                        fromfile="改动前", tofile=f"当前 · {rel}",
                                        lineterm="", n=2)
            text = "\n".join(diff)
            parts.append(f"### {rel}\n" + (text[:3000] or "（与当前无差异）"))
        out = "\n\n".join(parts) or "（无文件）"
        if len(out) > max_chars:
            out = out[:max_chars] + "\n…（截断）"
        return {"ok": True, "diff": out}

    def _usage_budget_warning(self) -> str:
        """今日 token 用量相对预算（config.usage_budget，0/缺省=不限）的提示文案。"""
        try:
            budget = int(self._config().get("usage_budget") or 0)
        except (TypeError, ValueError):
            budget = 0
        if budget <= 0:
            return ""
        from .exporter import usage_records

        today = time.strftime("%Y-%m-%d")
        total = 0
        for rec in usage_records(self.host.profile):
            ts = rec.get("ts")
            if not isinstance(ts, (int, float)):
                continue
            if time.strftime("%Y-%m-%d", time.localtime(ts)) != today:
                continue
            total += int(rec.get("prompt_tokens") or 0) + int(rec.get("completion_tokens") or 0)
        if total < budget:
            return ""
        pct = int(total * 100 / budget)
        if pct >= 100:
            return f"⚠ 今日 token 用量已超预算：{total} / {budget}（{pct}%）"
        return f"今日 token 用量已达预算 {pct}%（{total} / {budget}）"

    def export_profile(self, dest: str = "") -> dict[str, Any]:
        """把 profile 关键数据（配置/会话/记忆/定时任务/知识库索引/用量）打包为 zip。

        dest 省略时落到 <工作区>/exports/profile-backup-<时间>.zip。
        不包含 plugins/（外部插件用 `sha plugin add` 重装即可，避免大 zip）。
        """
        import zipfile

        root = Path(self.host.profile.root)
        dest = str(dest or "").strip()
        if dest:
            target = Path(dest)
        else:
            out_dir = Path(self.host.workspace or ".") / "exports"
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / f"profile-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        entries: list[tuple[Path, str]] = []
        for name in ("config.json", "memory.md", "usage.jsonl", "subagents.jsonl"):
            p = root / name
            if p.is_file():
                entries.append((p, name))
        for sub, arc in ((self.host.profile.sessions_dir, "sessions"),
                         (root / "scheduled", "scheduled"),
                         (root / "knowledge", "knowledge")):
            if Path(sub).is_dir():
                for f in Path(sub).rglob("*"):
                    if f.is_file():
                        entries.append((f, f"{arc}/{f.relative_to(Path(sub)).as_posix()}"))
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for src, arcname in entries:
                zf.write(src, arcname)
        return {"ok": True, "path": str(target), "files": len(entries)}

    def import_profile(self, src: str) -> dict[str, Any]:
        """从备份 zip 恢复 profile（覆盖同名文件；当前 config 先备份为 config.pre-import.json）。

        只接受白名单成员（config.json / memory.md / usage.jsonl / subagents.jsonl /
        sessions/ / scheduled/ / knowledge/ 前缀），路径成分含 .. 或绝对路径的一律跳过。
        """
        import shutil
        import zipfile

        path = Path(str(src or "").strip())
        if not path.is_file():
            return {"ok": False, "error": f"文件不存在: {src}"}
        root = Path(self.host.profile.root)
        allowed_prefixes = ("sessions/", "scheduled/", "knowledge/")
        restored = skipped = 0
        try:
            with zipfile.ZipFile(path) as zf:
                cfg_backup = root / "config.pre-import.json"
                if (root / "config.json").is_file():
                    shutil.copy2(root / "config.json", cfg_backup)
                for name in zf.namelist():
                    if name.endswith("/"):
                        continue
                    norm = name.replace("\\", "/")
                    if norm.startswith(("/", "~")) or ".." in Path(norm).parts:
                        skipped += 1
                        continue
                    top = norm.split("/", 1)[0]
                    if norm not in ("config.json", "memory.md", "usage.jsonl", "subagents.jsonl") \
                            and f"{top}/" not in allowed_prefixes:
                        skipped += 1
                        continue
                    dest = root / norm
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(name))
                    restored += 1
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"导入失败: {type(exc).__name__}: {exc}"}
        reset = self.host.service("reset_agent")
        if callable(reset):
            reset()
        return {"ok": True, "restored": restored, "skipped": skipped,
                "note": "下一轮对话生效；如异常可用 config.pre-import.json 还原"}

    def chat_stream(self, message: str, session_id: str = "",
                    images: list | None = None) -> dict[str, Any]:
        """流式对话（功能：桌面端流式输出）。

        立即返回（{"ok": True, "stream": True}），真正的对话在后台线程执行；
        delta / tool / done 事件经 evaluate_js 推给 ``window.onStreamEvent``：
        - {"kind": "delta", "text"}          —— 模型文本增量
        - {"kind": "done", ok, reply, ...}   —— 结束（载荷与整段 chat 的返回一致）
        - 出错时 {"kind": "done", ok: False, "error"}
        模型运作过程（思考/工具）仍走既有 ``onAgentEvent`` 通道，复用 live block UI。
        """
        ask_stream = self.host.service("ask_stream")
        if not callable(ask_stream):
            return {"ok": False, "error": "对话插件未激活或模型不支持流式"}
        self._ensure_session_workspace(session_id or self._current_session)
        if session_id in self._busy_sessions:
            return {"ok": False, "error": "该会话已有对话在进行中"}
        session_id = session_id or self._current_session
        with self._lock:
            if session_id != self._current_session:
                reset = self.host.service("new_session")
                if callable(reset):
                    reset(session_id)
                self._current_session = session_id

        def worker() -> None:
            self._busy_sessions.add(session_id)
            self._cancel_sessions.discard(session_id)  # 清掉上次残留的中断请求

            def _should_stop() -> bool:
                return session_id in self._cancel_sessions

            # 思考/工具步骤 → 界面（带会话 id：多会话并行时前端按会话过滤）
            self.host.emit_agent_event = (
                lambda payload, _sid=session_id:
                self._push_event({**payload, "session": _sid}))
            partial: list[str] = []   # 中断时已收到的部分回复（回给界面收尾）
            try:
                final_payload = None
                for event in ask_stream(message, session_id=session_id,
                                        images=images or None, should_stop=_should_stop):
                    if event.get("type") == "delta":
                        partial.append(event.get("text") or "")
                        self._push_stream_event({"kind": "delta", "text": event.get("text") or "",
                                                 "session": session_id})
                    elif event.get("type") == "done":
                        result = event.get("result")
                        runtime = self.host.service("models_runtime") or {}
                        # 暂存 done，等生成器收尾（会话落盘 + 用量补记）后再推——
                        # 保证前端收到 done 时切回该会话一定能加载到完整消息
                        final_payload = {
                            "kind": "done", "ok": True,
                            "reply": getattr(result, "content", ""),
                            "reasoning": getattr(result, "reasoning", ""),
                            "tool_calls": getattr(result, "tool_calls", []) or [],
                            "model": runtime.get("current", ""),
                            "usage": getattr(result, "usage", {}),
                            "session": session_id,
                        }
                if final_payload is not None:
                    # 自动钩子（config.hooks.reply_end）：流式路径在推送前补跑并附到回复尾
                    hook_svc = self.host.service("run_hooks")
                    if callable(hook_svc):
                        try:
                            hook_text = hook_svc()
                            if hook_text:
                                final_payload["reply"] = (final_payload.get("reply") or "") + hook_text
                        except Exception:  # noqa: BLE001 —— 钩子失败不影响回复
                            pass
                    warn = self._usage_budget_warning()
                    if warn:
                        final_payload["budget_warning"] = warn
                    self._push_stream_event(final_payload)
                    # 正常收尾后顺手给默认命名的会话起标题（锁外线程，不挡其他会话）
                    self._maybe_auto_title(session_id, message,
                                           str(final_payload.get("reply") or ""))
                elif _should_stop():
                    # 用户点了停止：nanoagent 不产出 done，这里补一个 cancelled 收尾——
                    # 部分内容不落盘（与 ask 中断即不落盘一致），只回给界面展示
                    self._push_stream_event({
                        "kind": "done", "ok": True, "cancelled": True,
                        "reply": "".join(partial), "reasoning": "",
                        "tool_calls": [], "model": "", "usage": {},
                        "session": session_id,
                    })
            except Exception as exc:  # noqa: BLE001 —— 错误回到界面而不是崩窗口
                self._push_stream_event({"kind": "done", "ok": False,
                                         "error": str(exc), "session": session_id})
            finally:
                self.host.emit_agent_event = None
                self._busy_sessions.discard(session_id)
                self._cancel_sessions.discard(session_id)

        threading.Thread(target=worker, daemon=True, name="sha-chat-stream").start()
        self._touch_session(session_id)
        return {"ok": True, "stream": True, "session": session_id}

    def stop_stream(self, session_id: str = "") -> dict[str, Any]:
        """请求中断某会话正在进行的流式回复。

        nanoagent 在每轮开始与每个 delta 之间检查取消标志——当前这一个
        LLM/工具步骤会先跑完，随后停止产出（部分内容不落盘）。
        """
        sid = str(session_id or "").strip() or self._current_session
        self._cancel_sessions.add(sid)
        return {"ok": True, "session": sid}

    def _push_event(self, payload: dict[str, Any]) -> None:
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                "window.onAgentEvent && window.onAgentEvent(" + json.dumps(payload) + ")"
            )
        except Exception:  # noqa: BLE001 —— 推送失败不影响对话
            pass

    def _push_stream_event(self, payload: dict[str, Any]) -> None:
        """流式 delta/done 事件 → 前端 ``window.onStreamEvent``（独立于步骤事件通道）。"""
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                "window.onStreamEvent && window.onStreamEvent(" + json.dumps(payload) + ")"
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
            effort = level or None  # off 也透传（llm 端转为 enable_thinking=false）
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
        source = "llm"
        if not (int(total.get("prompt_tokens") or 0)
                or int(total.get("completion_tokens") or 0)):
            # 服务商流式响应不带 usage 帧时 total_usage 恒为零——
            # 兜底用 usage.jsonl 的今日汇总（含估算轮），输入框下方才有数据可显示
            import datetime as _dt

            from .exporter import usage_records

            today = _dt.date.today().isoformat()
            agg = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
            for rec in usage_records(self.host.profile):
                if rec.get("ts") and _dt.date.fromtimestamp(float(rec["ts"])).isoformat() != today:
                    continue
                agg["prompt_tokens"] += int(rec.get("prompt_tokens") or 0)
                agg["completion_tokens"] += int(rec.get("completion_tokens") or 0)
                agg["cached_tokens"] += int(rec.get("cached_tokens") or 0)
            total = agg
            source = "usage.jsonl(今日)"
        return {"ok": True, "usage": total, "source": source}

    def export_session(self, session_id: str | None = None) -> dict[str, Any]:
        """把当前（或指定）会话导出为 Markdown → ``<工作区>/exports/<会话>.md``。

        与 CLI ``sha export`` 共用 exporter 模块；返回目标路径供界面提示。
        """
        from .exporter import export_session_markdown

        sid = str(session_id or self._current_session or "").strip()
        if not sid:
            return {"ok": False, "error": "没有可导出的会话"}
        try:
            dest = export_session_markdown(
                self.host.profile, sid, Path(self.host.workspace) / "exports")
        except FileNotFoundError:
            return {"ok": False, "error": f"会话为空或不存在: {sid}"}
        except OSError as exc:
            return {"ok": False, "error": f"写入失败: {exc}"}
        return {"ok": True, "path": str(dest)}

    def export_all_sessions(self) -> dict[str, Any]:
        """全部会话打包导出为 zip（每会话一个 Markdown + 目录）→ ``<工作区>/exports``。"""
        from .exporter import export_all_sessions_zip

        dest = (Path(self.host.workspace) / "exports"
                / f"sessions-{time.strftime('%Y%m%d-%H%M%S')}.zip")
        try:
            export_all_sessions_zip(self.host.profile, dest)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": f"写入失败: {exc}"}
        return {"ok": True, "path": str(dest)}

    def usage_daily(self, days: int = 30) -> dict[str, Any]:
        """按天聚合 token 用量（usage.jsonl → 折线图数据）。"""
        from .exporter import usage_daily as _daily

        try:
            points = _daily(self.host.profile, days)
        except Exception as exc:  # noqa: BLE001 —— 统计失败不影响界面
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "days": points}

    # -- 工作区 TODO.md 任务索引（F1）-------------------------------------------
    def todo_content(self) -> dict[str, Any]:
        """读工作区根 TODO.md 原文（任务面板渲染用；agent 经 todo_write 维护）。"""
        ws = str(getattr(self.host, "workspace", "") or "").strip()
        if not ws:
            return {"ok": False, "error": "未选择工作区"}
        path = Path(ws) / "TODO.md"
        if not path.is_file():
            return {"ok": True, "exists": False, "content": ""}
        try:
            return {"ok": True, "exists": True, "path": str(path),
                    "content": path.read_text(encoding="utf-8")}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def stateless_reset(self) -> dict[str, Any]:
        """无状态重置（F6）：旧会话总结进展写回 TODO.md → 自动开全新会话。

        旧会话保留在磁盘上（可 /resume 回看）；新会话靠 F3 自动恢复机制
        读 TODO.md 未完成项与 git log 接续执行。
        """
        old = self._current_session
        result = self.chat(
            "请立即调用 todo_write 工具，把本次会话已完成的进展与剩余工作整理为"
            "待办清单（已完成项 status=done，剩余项 status=pending，"
            "含简短 detail），然后只回复：已写入 TODO.md")
        created = self.new_session()
        note = str(result.get("reply") or "").strip()[:120] if result.get("ok") else ""
        return {"ok": bool(result.get("ok")), "old": old,
                "session": str(created.get("session") or ""),
                "note": note, "error": str(result.get("error") or "")}

    def bootstrap_project(self, goal: str = "") -> dict[str, Any]:
        """初始化工程（F5）：目录规范 + .gitignore + git 基线提交。

        给了工程目标时，再用一轮对话让 agent 把目标拆解为原子任务写入 TODO.md。
        """
        from .bootstrap import run_bootstrap

        ws = str(getattr(self.host, "workspace", "") or "").strip()
        if not ws:
            return {"ok": False, "error": "未选择工作区"}
        goal = str(goal or "").strip()
        res = run_bootstrap(ws, goal)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "脚手架失败",
                    "created": res.get("created") or []}
        todo_note = ""
        if goal:
            result = self.chat(
                f"工程目标：{goal}\n"
                "请立即调用 todo_write 工具，把该目标拆解为 3~8 个原子任务写入"
                " TODO.md（status=pending，每项带简短 detail），然后只回复："
                "已生成 TODO.md")
            if result.get("ok"):
                todo_note = str(result.get("reply") or "").strip()[:120]
            else:
                todo_note = "（TODO.md 生成失败: " + str(result.get("error") or "") + "）"
        return {"ok": True, "created": res["created"],
                "committed": res["committed"],
                "commit_error": res.get("error") or "",
                "todo_note": todo_note}

    # -- 知识库（knowledge 插件以 host.service("knowledge") 暴露能力） ----------
    def _knowledge_service(self) -> dict[str, Any] | None:
        service = self.host.service("knowledge")
        return service if isinstance(service, dict) else None

    def knowledge_status(self) -> dict[str, Any]:
        svc = self._knowledge_service()
        if svc is None:
            return {"ok": False,
                    "error": "知识库插件未激活（检查 config.knowledge.enabled 与 nanoagent 安装）"}
        info = dict(svc["status"]())
        info["ok"] = True
        return info

    def knowledge_pick_index(self) -> dict[str, Any]:
        """弹原生文件夹选择框，把所选目录索引进知识库（用户亲自选择，允许工作区外）。"""
        if self._window is None:
            return {"ok": False, "error": "窗口未就绪"}
        svc = self._knowledge_service()
        if svc is None:
            return {"ok": False,
                    "error": "知识库插件未激活（检查 config.knowledge.enabled 与 nanoagent 安装）"}
        try:
            import webview

            picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:  # noqa: BLE001 —— 用户取消或对话框失败
            return {"ok": False, "error": str(exc)}
        if not picked:
            return {"ok": False, "error": "已取消"}
        path = picked[0] if isinstance(picked, (list, tuple)) else picked
        summary = str(svc["index_absolute"](str(path)))
        return {"ok": not summary.startswith("错误"), "summary": summary}

    def knowledge_clear(self) -> dict[str, Any]:
        svc = self._knowledge_service()
        if svc is None:
            return {"ok": False,
                    "error": "知识库插件未激活（检查 config.knowledge.enabled 与 nanoagent 安装）"}
        summary = str(svc["clear"]())
        return {"ok": True, "summary": summary}

    def knowledge_sources(self) -> dict[str, Any]:
        """列出知识库已索引的来源文件（细粒度管理）。"""
        svc = self._knowledge_service()
        if svc is None:
            return {"ok": False,
                    "error": "知识库插件未激活（检查 config.knowledge.enabled 与 nanoagent 安装）"}
        try:
            return {"ok": True, "sources": list(svc["sources"]())}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def knowledge_remove_source(self, name: str) -> dict[str, Any]:
        """删除知识库里某个来源文件的全部片段。"""
        svc = self._knowledge_service()
        if svc is None:
            return {"ok": False,
                    "error": "知识库插件未激活（检查 config.knowledge.enabled 与 nanoagent 安装）"}
        summary = str(svc["remove_source"](name))
        return {"ok": not summary.startswith("错误"), "summary": summary}

    # -- 快捷指令（提示词片段库，桌面端与 REPL /snip 共用） ----------------------
    def snippets_list(self) -> dict[str, Any]:
        from .snippets import snippets_list

        return {"ok": True, "snippets": snippets_list(self.host.profile)}

    def snippets_save(self, name: str, text: str) -> dict[str, Any]:
        from .snippets import snippets_save

        return snippets_save(self.host.profile, name, text)

    def snippets_delete(self, name: str) -> dict[str, Any]:
        from .snippets import snippets_delete

        return snippets_delete(self.host.profile, name)

    # -- 工作区 git 远程配置（用户手动操作入口；agent 无 push 能力） -------------
    def _git_run(self, *args: str, timeout: int = 60) -> tuple[int, str]:
        """在工作区跑 git，返回 (退出码, 输出)（utf8→gbk 兜底解码）。"""
        ws = str(getattr(self.host, "workspace", "") or "").strip()
        if not ws:
            return 1, "未选择工作区"
        import subprocess as _sp

        try:
            proc = _sp.run(["git", "-c", "core.quotepath=false", *args],
                           cwd=ws, capture_output=True, timeout=timeout, check=False,
                           creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            return 1, "本机没有 git 命令"
        except _sp.TimeoutExpired:
            return 1, f"git 操作超时（{timeout}s）"
        out = proc.stdout.decode("utf-8", "replace")
        if "\ufffd" in out:
            try:
                out = proc.stdout.decode("gbk", "replace")
            except LookupError:
                pass
        err = proc.stderr.decode("utf-8", "replace")
        if proc.returncode != 0:
            out = (out.strip() + ("\n" if out.strip() and err.strip() else "")
                   + err.strip()).strip()
        return proc.returncode, out or "（无输出）"

    def git_remote_get(self) -> dict[str, Any]:
        """读取工作区 origin 远程地址（审查面板回显用）。"""
        code, out = self._git_run("remote", "get-url", "origin")
        return {"ok": True, "configured": code == 0,
                "url": out.strip() if code == 0 else ""}

    def git_remote_set(self, url: str) -> dict[str, Any]:
        """配置工作区 origin 远程地址（提交/推送的目标仓库；用户手动触发）。"""
        url = str(url or "").strip()
        import re as _re

        if not _re.match(r"^(https?://|ssh://|git@)\S+$", url):
            return {"ok": False,
                    "error": "远程地址格式不对（支持 https:// / ssh:// / git@ 开头）"}
        code, _ = self._git_run("remote", "get-url", "origin")
        if code == 0:
            code2, err = self._git_run("remote", "set-url", "origin", url)
            action = "已更新"
        else:
            code2, err = self._git_run("remote", "add", "origin", url)
            action = "已添加"
        if code2 != 0:
            return {"ok": False, "error": err}
        return {"ok": True, "url": url, "action": action}

    def git_push(self) -> dict[str, Any]:
        """推送当前分支到 origin（用户在审查面板手动点击；agent 无 push 能力）。"""
        code, out = self._git_run("remote", "get-url", "origin")
        if code != 0:
            return {"ok": False, "output": "尚未配置远程仓库（origin）——"
                    "先在上方填写远程地址并保存"}
        code, out = self._git_run("push", "-u", "origin", "HEAD", timeout=300)
        return {"ok": code == 0, "output": out}

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
    # 文件预览：扩展名分组（未列出的按内容嗅探：可解码即按文本，否则二进制）
    IMG_EXT: ClassVar[set[str]] = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
               ".svg", ".avif", ".tif", ".tiff"}
    MD_EXT: ClassVar[set[str]] = {".md", ".markdown", ".mdown", ".mkd", ".rst"}
    DATA_EXT: ClassVar[set[str]] = {".json", ".jsonl", ".jsonc", ".yml", ".yaml", ".toml", ".ini",
                ".cfg", ".conf", ".xml", ".properties", ".env"}
    TABLE_EXT: ClassVar[set[str]] = {".csv", ".tsv"}
    OFFICE_EXT: ClassVar[set[str]] = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                  ".wps", ".odt", ".ods", ".odp", ".pages", ".numbers"}
    ARCHIVE_EXT: ClassVar[set[str]] = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".jar",
                   ".whl", ".iso", ".apk"}
    MEDIA_EXT: ClassVar[set[str]] = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".mp4", ".mov",
                 ".avi", ".mkv", ".webm", ".flv"}
    AUDIO_EXT: ClassVar[set[str]] = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus"}
    VIDEO_EXT: ClassVar[set[str]] = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v"}
    HTML_EXT: ClassVar[set[str]] = {".html", ".htm", ".xhtml"}
    # 可在应用内解析出内容的 Office 子集（OOXML：zip + xml）
    OOXML_TEXT_EXT: ClassVar[dict[str, str]] = {".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx"}
    CODE_EXT: ClassVar[set[str]] = {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
                ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".java",
                ".kt", ".swift", ".rb", ".php", ".saho", ".sh", ".bash", ".zsh",
                ".ps1", ".bat", ".sql", ".lua", ".r", ".m", ".scala", ".dart",
                ".vue", ".svelte", ".html", ".htm", ".css", ".scss", ".less",
                ".txt", ".log", ".gitignore", ".dockerfile", ".editorconfig"}

    def _safe_ws_path(self, rel_path: str) -> tuple[Path | None, str]:
        """把相对路径解析为工作区内的绝对路径（越界返回错误）。"""
        ws = str(getattr(self.host, "workspace", "") or "").strip()
        if not ws:
            return None, "未选择工作区"
        root = Path(ws).resolve()  # 必须 resolve，否则相对工作区下无法 relative_to
        target = (root / str(rel_path or "")).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None, "路径越出工作区"
        return target, ""

    # ---- 预览辅助：无第三方依赖地解析常见二进制容器 ----

    @staticmethod
    def _docx_text(raw: bytes) -> str:
        """docx → 纯文本（zip 里取 word/document.xml，段落转行）。"""
        import io
        import re as _re
        import zipfile

        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
        xml = xml.replace("</w:p>", "\n").replace("<w:br/>", "\n")
        return _re.sub(r"<[^>]+>", "", xml).strip()

    @staticmethod
    def _xlsx_text(raw: bytes, max_rows: int = 300) -> str:
        """xlsx → TSV 文本（共享字符串表 + 首个工作表，最多取前 max_rows 行）。"""
        import io
        import re as _re
        import zipfile

        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                sx = zf.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                shared = [_re.sub(r"<[^>]+>", "", m)
                          for m in _re.findall(r"<si>(.*?)</si>", sx, _re.S)]
            sheet = "xl/worksheets/sheet1.xml"
            if sheet not in names:
                sheets = sorted(n for n in names
                                if n.startswith("xl/worksheets/") and n.endswith(".xml"))
                if not sheets:
                    return ""
                sheet = sheets[0]
            sx = zf.read(sheet).decode("utf-8", "replace")
        lines = []
        for row in _re.findall(r"<row[^>]*>(.*?)</row>", sx, _re.S)[:max_rows]:
            cells = []
            for m in _re.finditer(r"<c([^>]*)>(.*?)</c>", row, _re.S):
                attrs, inner = m.group(1), m.group(2)
                v = _re.search(r"<v>(.*?)</v>", inner, _re.S)
                val = (v.group(1) if v else "").strip()
                if 't="s"' in attrs and val.isdigit():
                    idx = int(val)
                    val = shared[idx] if 0 <= idx < len(shared) else val
                cells.append(val)
            lines.append("\t".join(cells))
        return "\n".join(lines)

    @staticmethod
    def _pptx_text(raw: bytes, max_slides: int = 60) -> str:
        """pptx → 每页一段文本（取 ppt/slides 的 a:t 节点）。"""
        import io
        import re as _re
        import zipfile

        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            slides = sorted(n for n in zf.namelist()
                            if _re.match(r"ppt/slides/slide\d+\.xml$", n))
            out = []
            for i, name in enumerate(slides[:max_slides], 1):
                xml = zf.read(name).decode("utf-8", "replace")
                texts = [_re.sub(r"<[^>]+>", "", t)
                         for t in _re.findall(r"<a:t>(.*?)</a:t>", xml, _re.S)]
                out.append(f"── 第 {i} 页 ──\n" + "\n".join(t for t in texts if t.strip()))
        return "\n\n".join(out)

    @staticmethod
    def _zip_listing(raw: bytes, max_entries: int = 500) -> str:
        """压缩包 → 条目清单（名称 / 大小）。"""
        import io
        import zipfile

        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                infos = zf.infolist()
                lines = [f"{'目录' if i.is_dir() else '文件':<4} {i.file_size:>12,}  {i.filename}"
                         for i in infos[:max_entries]]
                if len(infos) > max_entries:
                    lines.append(f"... 共 {len(infos)} 个条目，仅显示前 {max_entries} 个")
                return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 —— 非 zip 容器（rar/7z 等）
            return f"（无法列出该压缩包内容：{exc}）"

    @staticmethod
    def _hexdump(raw: bytes, limit: int = 2048) -> str:
        """二进制 → 十六进制摘要（偏移 / hex / ASCII，前 limit 字节）。"""
        out = []
        for off in range(0, min(len(raw), limit), 16):
            chunk = raw[off:off + 16]
            hexs = " ".join(f"{b:02x}" for b in chunk)
            asci = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            out.append(f"{off:08x}  {hexs:<47}  {asci}")
        if len(raw) > limit:
            out.append(f"… 共 {len(raw):,} 字节，仅显示前 {limit} 字节")
        return "\n".join(out)

    @staticmethod
    def _media_mime(ext: str, kind: str) -> str:
        table = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
                 ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac",
                 ".opus": "audio/opus", ".mp4": "video/mp4", ".mov": "video/quicktime",
                 ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
                 ".webm": "video/webm", ".flv": "video/x-flv", ".m4v": "video/mp4",
                 ".pdf": "application/pdf"}
        return table.get(ext, "video/mp4" if kind == "video" else "application/octet-stream")

    def file_preview(self, rel_path: str = "") -> dict[str, Any]:
        """读取工作区内文件用于预览：文本/代码/图片/Markdown/CSV/二进制元信息。

        - 路径锁定在工作区内部（与 ws_tree 同款越界校验）
        - 文本类限 256KB（超出截断并置 truncated 标记），图片转 data URL（≤2MB）
        - 无法呈现的类型（Office / 压缩包 / 音视频 / PDF）只返回元信息 + 建议外部打开
        """
        target, err = self._safe_ws_path(rel_path)
        if target is None:
            return {"ok": False, "error": err}
        if not target.is_file():
            return {"ok": False, "error": "文件不存在"}
        stat = target.stat()
        ext = target.suffix.lower()
        base = {"ok": True, "name": target.name, "rel": str(rel_path or target.name),
                "size": stat.st_size, "mtime": stat.st_mtime, "ext": ext}

        def kind_of() -> str:
            if ext in self.IMG_EXT:
                return "image"
            if ext in self.MD_EXT:
                return "markdown"
            if ext in self.TABLE_EXT:
                return "table"
            if ext in self.HTML_EXT:
                return "html"
            if ext in self.DATA_EXT:
                return "code"
            if ext == ".pdf":
                return "pdf"
            if ext in self.OFFICE_EXT:
                return "office"
            if ext in self.ARCHIVE_EXT:
                return "archive"
            if ext in self.AUDIO_EXT:
                return "audio"
            if ext in self.VIDEO_EXT:
                return "video"
            if ext in self.CODE_EXT or target.name.lower() in {
                    "dockerfile", "makefile", "cmakelists.txt", "license", "readme"}:
                return "code"
            return "unknown"

        kind = kind_of()

        if kind == "image" and stat.st_size <= 2 * 1024 * 1024:
            try:
                import base64

                mime = {".png": "image/png", ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg", ".gif": "image/gif",
                        ".webp": "image/webp", ".bmp": "image/bmp",
                        ".ico": "image/x-icon", ".svg": "image/svg+xml"}.get(
                            ext, "application/octet-stream")
                raw = target.read_bytes()
                return {**base, "kind": "image", "mime": mime,
                        "data_url": "data:" + mime + ";base64," +
                        base64.b64encode(raw).decode("ascii")}
            except Exception as exc:  # noqa: BLE001 —— 读失败退回元信息
                return {**base, "kind": kind, "error": str(exc)}

        if kind in {"pdf", "audio", "video"}:
            # 可内嵌渲染：转 data URL（按类型设上限，防超大文件把内存打爆）
            cap_mb = {"pdf": 12, "audio": 16, "video": 48}[kind]
            if stat.st_size > cap_mb * 1024 * 1024:
                return {**base, "kind": kind, "too_large": True,
                        "note": f"文件超过 {cap_mb}MB 内嵌上限，请用系统程序打开"}
            try:
                import base64

                mime = self._media_mime(ext, kind)
                raw = target.read_bytes()
                return {**base, "kind": kind, "mime": mime,
                        "data_url": "data:" + mime + ";base64," +
                        base64.b64encode(raw).decode("ascii")}
            except Exception as exc:  # noqa: BLE001 —— 读失败退回元信息
                return {**base, "kind": kind, "error": str(exc)}

        if kind == "office":
            # OOXML（docx/xlsx/pptx）在应用内解析出文本/表格；老格式只给元信息
            parser = self.OOXML_TEXT_EXT.get(ext)
            if not parser or stat.st_size > 8 * 1024 * 1024:
                return {**base, "kind": "office"}
            try:
                raw = target.read_bytes()
                if parser == "docx":
                    doc = self._docx_text(raw)
                    return {**base, "kind": "office", "text": doc,
                            "lines": doc.count("\n") + 1,
                            "note": "Word 文档正文（无排版）"}
                if parser == "xlsx":
                    doc = self._xlsx_text(raw)
                    return {**base, "kind": "table", "text": doc,
                            "ext": ".tsv", "note": "Excel 首个工作表（最多 300 行）"}
                doc = self._pptx_text(raw)
                return {**base, "kind": "office", "text": doc,
                        "note": "PowerPoint 各页文本"}
            except Exception as exc:  # noqa: BLE001 —— 损坏/加密文档退回元信息
                return {**base, "kind": "office", "error": str(exc)}

        if kind == "archive":
            if stat.st_size <= 8 * 1024 * 1024:
                try:
                    raw = target.read_bytes()
                    listing = self._zip_listing(raw)
                    return {**base, "kind": "archive", "text": listing,
                            "note": "压缩包条目清单"}
                except Exception as exc:  # noqa: BLE001
                    return {**base, "kind": "archive", "error": str(exc)}
            return {**base, "kind": "archive"}

        # 其余按内容嗅探：不可解码的当二进制（给十六进制摘要）
        max_bytes = 256 * 1024
        try:
            raw = target.read_bytes()
        except Exception as exc:  # noqa: BLE001
            return {**base, "kind": kind, "error": str(exc)}
        head = raw[:max_bytes + 1]
        truncated = len(raw) > max_bytes
        if b"\0" in head:
            return {**base, "kind": "binary", "text": self._hexdump(raw),
                    "note": "二进制十六进制摘要"}
        text = None
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                text = head.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return {**base, "kind": "binary"}
        if truncated:
            text = text[:max_bytes]
        return {**base, "kind": kind, "text": text, "truncated": truncated,
                "lines": text.count("\n") + 1}

    def open_external(self, rel_path: str = "") -> dict[str, Any]:
        """用系统默认程序打开工作区内的文件（预览不了的类型用它兜底）。"""
        import os
        import subprocess

        target, err = self._safe_ws_path(rel_path)
        if target is None:
            return {"ok": False, "error": err}
        if not target.is_file():
            return {"ok": False, "error": "文件不存在"}
        try:
            if os.name == "nt":
                os.startfile(str(target))  # 就是要用默认程序打开
            elif sys.platform == "darwin":
                from .procutil import CREATE_NO_WINDOW

                subprocess.Popen(["open", str(target)],
                                 creationflags=CREATE_NO_WINDOW)
            else:
                from .procutil import CREATE_NO_WINDOW

                subprocess.Popen(["xdg-open", str(target)],
                                 creationflags=CREATE_NO_WINDOW)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(target)}

    def subagents(self, limit: int = 50) -> dict[str, Any]:
        """列出子 agent 调用记录（profile/subagents.jsonl，最新在前）。"""
        import json

        path = self.host.profile.root / "subagents.jsonl"
        if not path.is_file():
            return {"ok": True, "items": []}
        items: list[dict[str, Any]] = []
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        items.reverse()
        return {"ok": True, "items": items[: max(1, int(limit or 50))]}

    def remove_subagents(self, ids: list | None = None) -> dict[str, Any]:
        """移除子 agent 调用记录：ids 为空 → 清空全部；否则只删指定 id 的行。

        按 id 删时用「过滤 + 原子替换」重写 jsonl；坏行保留不丢数据。
        旧版记录没有 id 字段，只能走整表清空（前端对无 id 的行不渲染删除按钮）。
        """
        import json

        path = self.host.profile.root / "subagents.jsonl"
        if not path.is_file():
            return {"ok": True, "removed": 0}
        wanted = {str(i) for i in (ids or []) if str(i).strip()}
        try:
            if not wanted:
                path.unlink()
                return {"ok": True, "removed": "all"}
            kept: list[str] = []
            removed = 0
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(line)
                    continue
                if str(item.get("id") or "") in wanted:
                    removed += 1
                    continue
                kept.append(line)
            if removed:
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text("".join(l + "\n" for l in kept), encoding="utf-8")
                tmp.replace(path)
            return {"ok": True, "removed": removed}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    # -- 工作区文件引用 / 代码应用 -----------------------------------------------
    def ws_files(self, limit: int = 800) -> dict[str, Any]:
        """工作区文件扁平清单（@ 引用补全的数据源）：相对 posix 路径，跳过忽略目录。"""
        from .workspace import iter_files, relative

        items: list[str] = []
        try:
            for path in iter_files(self.host, "**/*",
                                   limit=max(1, min(int(limit or 800), 2000))):
                items.append(relative(self.host, path))
        except (ValueError, OSError):
            pass   # glob 模式异常 / 工作区不可达 → 空清单（补全功能不致命）
        return {"ok": True, "files": items}

    def ws_file_text(self, path: str, max_chars: int = 12000) -> dict[str, Any]:
        """读取工作区内文本文件原文（@ 引用展开用；路径监狱 + 大小双上限）。"""
        from .workspace import read_text_file, safe_path

        try:
            resolved = safe_path(self.host, str(path or ""), must_exist=True)
        except (ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        text = read_text_file(resolved)
        if text is None:
            return {"ok": False, "error": f"不是可读文本或超过大小上限: {path}"}
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n…（已截断，原文共 {len(text)} 字符）"
        return {"ok": True, "path": str(path), "text": text}

    def apply_code(self, path: str, content: str) -> dict[str, Any]:
        """把代码块写入工作区文件——复用 write_file 工具（权限门 + checkpoint 留底）。

        走同一条门意味着：fs=ask 照常弹确认、confirm_write 照常看 diff、
        写前照常留底（右侧 ↶ 撤销可用）。
        """
        tool = next((t for t in self.host.collect_tools() if t.name == "write_file"), None)
        if tool is None:
            return {"ok": False, "error": "文件工具未激活"}
        try:
            output = tool.invoke({"path": str(path or ""), "content": str(content or "")})
        except Exception as exc:  # noqa: BLE001 —— 不让异常越过 js_api 边界
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        text = str(output)
        return {"ok": not text.startswith("错误"), "output": text}

    def knowledge_query(self, query: str, k: int = 5) -> dict[str, Any]:
        """知识库试检索：直接调 search_knowledge 工具看命中（不进对话、不算用量）。"""
        if not str(query or "").strip():
            return {"ok": False, "error": "查询不能为空"}
        tool = next((t for t in self.host.collect_tools() if t.name == "search_knowledge"), None)
        if tool is None:
            return {"ok": False, "error": "知识库插件未激活"}
        try:
            output = tool.invoke({"query": str(query).strip(),
                                  "k": max(1, min(int(k or 5), 20))})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        text = str(output)
        return {"ok": not text.startswith("错误"), "output": text}

    # -- 定时任务面板 -------------------------------------------------------------
    def schedules(self) -> dict[str, Any]:
        """列出定时任务（与 scheduler 插件 / `sha schedule` 共用 tasks.json）。"""
        from .schedule_store import load_tasks

        try:
            return {"ok": True, "tasks": load_tasks(self.host.profile)}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def schedule_toggle(self, name: str, enabled: bool) -> dict[str, Any]:
        from .schedule_store import ScheduleError, set_task_enabled

        try:
            if not set_task_enabled(self.host.profile, str(name or ""), bool(enabled)):
                return {"ok": False, "error": f"没有任务: {name}"}
        except ScheduleError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "name": name, "enabled": bool(enabled)}

    def schedule_remove(self, name: str) -> dict[str, Any]:
        from .schedule_store import remove_task

        if remove_task(self.host.profile, str(name or "")):
            return {"ok": True, "name": name}
        return {"ok": False, "error": f"没有任务: {name}"}

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
                creationflags=CREATE_NO_WINDOW,
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

    # -- MCP 服务器管理 -----------------------------------------------------------
    def mcp_servers(self) -> dict[str, Any]:
        """列出配置的 MCP 服务器（headers/env 值掩码——回传掩码即保持原值）。"""
        servers = self._config().get("mcpServers") or {}
        items: list[dict[str, Any]] = []
        for name in sorted(servers):
            entry = dict(servers.get(name) or {})
            items.append({
                "name": name,
                "type": str(entry.get("type") or ("http" if entry.get("url") else "stdio")).lower(),
                "url": str(entry.get("url") or ""),
                "command": str(entry.get("command") or ""),
                "args": [str(a) for a in (entry.get("args") or [])],
                "headers": {k: _mask_key(v) for k, v in (entry.get("headers") or {}).items()},
                "env": {k: _mask_key(v) for k, v in (entry.get("env") or {}).items()},
            })
        return {"ok": True, "servers": items}

    def save_mcp(self, servers: list) -> dict[str, Any]:
        """保存 MCP 服务器声明并热重连（重载 mcp_client 插件 + 丢弃 agent 重建工具集）。

        headers/env 里形如掩码（含 ***）的值回传时替换回旧配置中的原值，
        与模型 API Key 的「留空/掩码即保持」约定一致。
        """
        old = self._config().get("mcpServers") or {}
        problems: list[str] = []
        cleaned: dict[str, dict[str, Any]] = {}

        def merge_secret(mapping: Any, prev: Any) -> dict[str, str]:
            out: dict[str, str] = {}
            for key, value in dict(mapping or {}).items():
                value = str(value or "").strip()
                if not value:
                    continue
                if "***" in value and str(key) in dict(prev or {}):
                    out[str(key)] = str(dict(prev or {})[str(key)])  # 掩码 → 原值
                else:
                    out[str(key)] = value
            return out

        for entry in servers or []:
            entry = entry or {}
            name = str(entry.get("name") or "").strip()
            if not name:
                problems.append("服务器名不能为空")
                continue
            if name in cleaned:
                problems.append(f"服务器重名: {name}")
                continue
            typ = str(entry.get("type") or "").strip().lower()
            url = str(entry.get("url") or "").strip()
            command = str(entry.get("command") or "").strip()
            if typ not in ("stdio", "http", "sse"):
                typ = "http" if url else "stdio"
            prev = dict(old.get(name) or {})
            if typ in ("http", "sse"):
                if not url.startswith(("http://", "https://")):
                    problems.append(f"{name}: URL 必须以 http(s):// 开头")
                    continue
                item: dict[str, Any] = {"url": url, "type": typ}
                headers = merge_secret(entry.get("headers"), prev.get("headers"))
                if headers:
                    item["headers"] = headers
            else:
                if not command:
                    problems.append(f"{name}: stdio 传输需要填写启动命令")
                    continue
                item = {"command": command}
                raw_args = entry.get("args")
                if isinstance(raw_args, str):
                    args = [a for a in raw_args.split() if a.strip()]
                else:
                    args = [str(a).strip() for a in (raw_args or []) if str(a).strip()]
                if args:
                    item["args"] = args
            env = merge_secret(entry.get("env"), prev.get("env"))
            if env:
                item["env"] = env
            cleaned[name] = item
        if problems:
            return {"ok": False, "error": "；".join(problems[:3])}

        self.host.profile.update_config(mcpServers=cleaned)
        reload_error = ""
        if "mcp_client" in self.host.plugins:
            try:
                self.host.reload("mcp_client")   # 按新配置断开重连（disposer 先回收旧连接）
            except Exception as exc:  # noqa: BLE001 —— 重连失败如实回给界面
                reload_error = f"{type(exc).__name__}: {exc}"
        reset = self.host.service("reset_agent")
        if callable(reset):
            reset()   # 丢弃 agent：下一轮 ask 聚合到新的 MCP 工具集
        return {"ok": True, "count": len(cleaned), "reload_error": reload_error}


# ---------------------------------------------------------------------------
# 渲染层（仅 UI；所有能力走 window.pywebview.api）
# 视觉对齐 dsh-desktop：近黑工作台、左侧栏、居中输入卡片、DeepSeek 蓝点缀
# ---------------------------------------------------------------------------

UI_HTML = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>卅 harness</title><style>
:root {
  --bg: #0c0d12; --panel: #12141c; --elev: #191c26; --hover: #232634;
  --line: #2b2e3d; --line-soft: #20222e;
  --fg: #eaebf3; --dim: #9497ab; --faint: #63667c;
  --accent: #5b7cfa; --accent-2: #8b5cf6;
  --accent-grad: linear-gradient(135deg, #5b7cfa 0%, #8b5cf6 100%);
  --accent-soft: rgba(91,124,250,.15);
  --ok: #5cc689; --err: #ef7b6d;
  --bubble: #20232f; --scroll: #353950; --shadow: rgba(4,6,16,.55);
  --radius: 14px;
  --ring: 0 0 0 3px rgba(91,124,250,.22);
  --shadow-lg: 0 16px 48px rgba(4,6,16,.55);
  --accent-glow: 0 4px 16px rgba(91,124,250,.35);
}
html[data-theme="light"] {
  --bg: #f3f4f9; --panel: #ffffff; --elev: #eef0f7; --hover: #e4e7f1;
  --line: #dbdeea; --line-soft: #e8eaf2;
  --fg: #181a24; --dim: #5d6176; --faint: #9195ab;
  --accent: #4d6bfe; --accent-2: #7c4dff;
  --accent-grad: linear-gradient(135deg, #4d6bfe 0%, #7c4dff 100%);
  --accent-soft: rgba(77,107,254,.10);
  --bubble: #e9ebf6; --scroll: #c8ccdc; --shadow: rgba(24,28,60,.12);
  --ring: 0 0 0 3px rgba(77,107,254,.16);
  --shadow-lg: 0 16px 44px rgba(24,28,60,.16);
  --accent-glow: 0 4px 16px rgba(77,107,254,.30);
}
@media (prefers-color-scheme: light) {
  html[data-theme="system"] {
    --bg: #f3f4f9; --panel: #ffffff; --elev: #eef0f7; --hover: #e4e7f1;
    --line: #dbdeea; --line-soft: #e8eaf2;
    --fg: #181a24; --dim: #5d6176; --faint: #9195ab;
    --accent: #4d6bfe; --accent-2: #7c4dff;
    --accent-grad: linear-gradient(135deg, #4d6bfe 0%, #7c4dff 100%);
    --accent-soft: rgba(77,107,254,.10);
    --bubble: #e9ebf6; --scroll: #c8ccdc; --shadow: rgba(24,28,60,.12);
    --ring: 0 0 0 3px rgba(77,107,254,.16);
    --shadow-lg: 0 16px 44px rgba(24,28,60,.16);
    --accent-glow: 0 4px 16px rgba(77,107,254,.30);
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin:0; font-family:'Segoe UI','Microsoft YaHei',system-ui,sans-serif;
       background:var(--bg); color:var(--fg); overflow:hidden;
       -webkit-font-smoothing: antialiased; }
::selection { background:var(--accent-soft); }
::-webkit-scrollbar { width:6px; height:6px; }
::-webkit-scrollbar-thumb { background:var(--scroll); border-radius:3px; }
::-webkit-scrollbar-thumb:hover { background:var(--dim); }
::-webkit-scrollbar-track { background:transparent; }
.app { display:flex; height:100vh; }

/* ---------- 侧栏 ---------- */
aside { width:260px; min-width:260px; background:var(--panel);
        border-right:1px solid var(--line-soft); display:flex; flex-direction:column;
        padding:14px 12px; }
.brand { display:flex; align-items:center; gap:10px; padding:2px 6px 14px; }
.logo { width:34px; height:34px; border-radius:10px; background:var(--accent-grad); color:#fff;
        display:flex; align-items:center; justify-content:center;
        font-weight:800; font-size:17px; font-family:Georgia,'Times New Roman',serif;
        box-shadow:var(--accent-glow); }
.brand b { font-size:13.5px; letter-spacing:.06em; display:block; }
.brand small { color:var(--faint); font-size:11px; letter-spacing:.04em; }
.new-btn { display:flex; align-items:center; justify-content:center; gap:8px;
           width:100%; padding:10px 0; border-radius:var(--radius); cursor:pointer;
           background:var(--accent-grad); border:none; color:#fff;
           font-size:13.5px; font-weight:600; box-shadow:var(--accent-glow);
           transition:filter .15s, transform .1s; }
.new-btn:hover { filter:brightness(1.1); }
.new-btn:active { transform:scale(.98); }
.side-label { color:var(--faint); font-size:11px; letter-spacing:.1em; margin:16px 8px 6px; }
.side-filter { padding:0 2px 6px; }
.side-filter input { width:100%; background:var(--bg); border:1px solid var(--line-soft);
                     border-radius:8px; color:var(--fg); padding:6px 10px; font-size:12px;
                     outline:none; transition:border-color .15s, box-shadow .15s; }
.side-filter input:focus { border-color:var(--accent); box-shadow:var(--ring); }
.side-filter input::placeholder { color:var(--faint); }
.side-list { flex:1; overflow-y:auto; margin:0 -4px; }
.ws-group { margin-bottom:2px; }
.ws-head { display:flex; align-items:center; gap:7px; padding:6px 10px; border-radius:8px;
           color:var(--dim); font-size:12.5px; cursor:pointer; user-select:none; }
.ws-head:hover { background:var(--hover); }
.ws-caret { flex:none; font-size:9px; color:var(--faint); transition:transform .15s;
            width:10px; text-align:center; border:none; background:transparent;
            padding:0; cursor:pointer; }
.ws-head.cur .ws-name { font-weight:600; color:var(--fg); }
/* 焦点唯一性：只让当前会话行带强调色（s-row.active），
   工作区头部只用加粗区分——避免「两个聚焦」的视觉歧义 */
.ws-head { position:relative; transition:background .12s; }
.ws-group.collapsed .ws-caret { transform:rotate(-90deg); }
.ws-group.collapsed .s-row { display:none; }
.ws-head .ws-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.row-menu { border:none; background:transparent; color:var(--faint); font-size:14px;
            cursor:pointer; border-radius:6px; padding:0 6px; visibility:hidden; }
.ws-head:hover .row-menu, .s-row:hover .row-menu { visibility:visible; }
.row-menu:hover { color:var(--fg); background:#333; }
.s-row .row-del:hover { color:#ff6b6b; }
.s-row { display:flex; align-items:center; gap:8px; padding:6px 10px 6px 26px;
         border-radius:8px; cursor:pointer; font-size:13px; color:var(--dim);
         user-select:none;
         transition:background .12s, color .12s; }
.s-row.dragging { opacity:.45; }
.s-row.dragover { outline:2px dashed var(--accent); outline-offset:-2px; }
/* 会话拖拽激活中：全局 grabbing 光标，明确「已进入拖拽」 */
body.sess-dragging, body.sess-dragging * { cursor:grabbing !important; }
.s-row:hover { background:var(--hover); color:var(--fg); }
.s-row.active { background:var(--accent-soft); color:var(--fg);
                box-shadow:inset 2.5px 0 0 var(--accent); }
.s-row .s-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.s-row .s-time { color:var(--faint); font-size:11px; flex:none; }
.side-bottom { border-top:1px solid var(--line-soft); padding-top:10px; margin-top:8px; }
.side-action { display:flex; align-items:center; gap:8px; width:100%; padding:8px 10px;
               border:none; background:transparent; color:var(--dim); font-size:13px;
               border-radius:8px; cursor:pointer; transition:background .12s, color .12s; }
.side-action:hover { background:var(--hover); color:var(--fg); }
.side-hint { color:var(--faint); font-size:11px; padding:6px 10px 0; line-height:1.5; }

/* ---------- 主区 ---------- */
main { flex:1; display:flex; flex-direction:column; min-width:0; min-height:0;
       background:var(--panel); }  /* 会话区与两侧同底色（此前露出 body 的 --bg，色阶更深） */
#hero { display:none; flex:1; flex-direction:column; align-items:center;
        justify-content:center; text-align:center; padding-bottom:8px; min-height:0; }
#hero .glyph { font-family:Georgia,'Times New Roman',serif; font-size:52px; font-weight:800;
               width:88px; height:88px; border-radius:24px; background:var(--accent-grad);
               color:#fff; border:none; display:flex; align-items:center;
               justify-content:center; margin-bottom:24px;
               box-shadow:0 14px 40px rgba(91,124,250,.35); }
#hero h1 { font-family:Georgia,'Times New Roman',serif; font-weight:700;
           font-size:36px; margin:0 0 12px; letter-spacing:.01em;
           background:linear-gradient(120deg, var(--fg) 40%, var(--dim));
           -webkit-background-clip:text; background-clip:text;
           -webkit-text-fill-color:transparent; }
#hero p { color:var(--dim); font-size:13.5px; margin:0; max-width:420px; line-height:1.7; }
#log { flex:1; overflow-y:auto; padding:26px 24px 10px; }
.thread { max-width:760px; margin:0 auto; }
.msg { position:relative; margin:0 0 14px; font-size:14px; line-height:1.65;
       word-break:break-word; user-select:text; -webkit-user-select:text; cursor:text;
       animation:msgIn .22s ease; }
@keyframes msgIn { from { opacity:0; transform:translateY(6px); }
                   to { opacity:1; transform:none; } }
.msg ::selection, .msg::selection { background:var(--accent-soft); }
.msg.user { background:var(--bubble); border:1px solid rgba(91,124,250,.22);
            border-radius:16px 16px 4px 16px; padding:10px 16px;
            max-width:78%; margin-left:auto; width:fit-content; white-space:pre-wrap; }
html[data-theme="light"] .msg.user { border-color:rgba(77,107,254,.20); }
/* ---------- 回答排版 v2（模型输出样式） ---------- */
.msg.bot { white-space:pre-wrap; line-height:1.7; }
.msg.bot code { background:var(--elev); border:1px solid var(--line); border-radius:5px;
                padding:1px 6px; font-family:Consolas,monospace; font-size:12.5px;
                color:var(--accent); }
.msg.bot .bold { font-weight:700; }
/* 标题：层级拉开——h1 底部分隔线、h2 强调色竖条、h3 强调色 */
.msg.bot .md-h { font-weight:700; line-height:1.45; margin:14px 0 5px; }
.msg.bot .md-h:first-child { margin-top:0; }
.msg.bot .md-h1 { font-size:17px; padding-bottom:3px; border-bottom:1px solid var(--line-soft); }
.msg.bot .md-h2 { font-size:15.5px; padding-left:9px; border-left:3px solid var(--accent); }
.msg.bot .md-h3 { font-size:14px; color:var(--accent); }
.msg.bot .md-h4 { font-size:14px; }
/* 列表：强调色项目符 + 行距；容器恢复正常空白（父级 pre-wrap 会把块间换行渲染成空行） */
.msg.bot ul, .msg.bot ol { margin:6px 0; padding-left:1.5em; white-space:normal; }
.msg.bot li { margin:3px 0; }
.msg.bot li::marker { color:var(--accent); }
/* 任务清单（- [ ] / - [x]） */
.msg.bot .task { color:var(--faint); font-weight:700; margin-right:2px; }
.msg.bot .task.done { color:var(--ok); }
/* 引用块：强调色竖条 + 微底色 */
.msg.bot blockquote { margin:8px 0; padding:6px 12px; border-left:3px solid var(--accent);
                      background:var(--elev); border-radius:0 8px 8px 0; white-space:normal; }
.msg.bot blockquote div { color:var(--dim); }
/* 链接 / 分隔线 */
.msg.bot a { color:var(--accent); text-decoration:underline; text-underline-offset:2px; }
.msg.bot a:hover { filter:brightness(1.15); }
.msg.bot hr { border:0; border-top:1px solid var(--line-soft); margin:12px 0; }
/* md 表格（模型结构化回答）：pre-wrap 上下文里必须恢复正常空白处理 */
.msg.bot .md-table { border-collapse:collapse; margin:8px 0; font-size:13px; white-space:normal; }
.msg.bot .md-table th, .msg.bot .md-table td { border:1px solid var(--line); padding:5px 10px; text-align:left; }
.msg.bot .md-table th { background:var(--elev); font-weight:700; border-bottom:2px solid var(--line); }
.msg.bot .md-table tbody tr:nth-child(even) td { background:var(--elev); }
/* 子 agent 调用记录（右侧「🤖 子agent」面板） */
.sub-row { border:1px solid var(--line-soft); border-radius:10px; margin:6px 0;
           overflow:hidden; background:var(--elev); }
.sub-head { display:flex; align-items:center; gap:7px; padding:7px 10px;
            cursor:pointer; }
.sub-head:hover { background:var(--hover); }
.sub-rm { margin-left:auto; flex:none; opacity:.55; line-height:1; padding:2px 7px; }
.sub-rm:hover { opacity:1; color:var(--err); }
.sub-badge { flex:none; width:16px; height:16px; border-radius:50%;
             display:flex; align-items:center; justify-content:center;
             font-size:10px; color:#fff; background:var(--ok); }
.sub-badge.bad { background:var(--err); }
.sub-task { flex:1; min-width:0; font-size:12px; color:var(--fg);
            overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sub-time { flex:none; color:var(--faint); font-size:11px; }
.sub-body { display:none; padding:2px 10px 10px; font-size:11.5px;
            color:var(--dim); line-height:1.6; }
.sub-row.open .sub-body { display:block; }
.sub-err { color:var(--err); margin-bottom:4px; }
.sub-steps { color:var(--accent); font-family:Consolas,monospace;
             margin-bottom:4px; word-break:break-all; }
.sub-step { padding:1px 0; }
.sub-step.bad { color:var(--err); }
.sub-step .sub-res { color:var(--faint); font-family:inherit; }
.sub-out { white-space:pre-wrap; word-break:break-word; }
.sub-meta { color:var(--faint); font-size:10.5px; margin-top:6px; }
/* 排队徽标：本轮回答中还允许输入，点发送即排队 */
.q-badge { flex:none; font-size:11px; color:var(--accent); background:var(--accent-soft);
           border-radius:10px; padding:3px 8px; white-space:nowrap; }
/* ---------- 权限审批弹窗（shell=ask / 写前确认） ---------- */
.ap-mask { display:none; position:fixed; inset:0; background:rgba(15,18,28,.42);
           z-index:80; align-items:center; justify-content:center; }
.ap-mask.open { display:flex; }
.ap-card { width:min(760px, 92vw); max-height:80vh; display:flex; flex-direction:column;
           background:var(--panel); border:1px solid var(--line); border-radius:16px;
           box-shadow:0 18px 60px var(--shadow); padding:16px 18px; }
.ap-head { display:flex; align-items:center; gap:10px; font-size:14px;
           font-weight:600; color:var(--fg); margin-bottom:10px; }
.ap-head .sp { flex:1; }
.ap-count { color:var(--faint); font-size:11.5px; font-weight:400; }
.ap-detail { flex:1; min-height:0; overflow:auto; margin-bottom:12px; }
.ap-detail .ap-cmd { background:var(--bg); border:1px solid var(--line-soft);
                     border-radius:10px; padding:10px 12px; font-size:12.5px;
                     font-family:Consolas,monospace; color:var(--fg);
                     white-space:pre-wrap; word-break:break-all; }
.ap-detail .ap-note { color:var(--dim); font-size:12px; margin:6px 0; }
.ap-detail .ap-diff { background:var(--bg); border:1px solid var(--line-soft);
                      border-radius:10px; padding:8px 10px; font-size:11.5px;
                      font-family:Consolas,monospace; line-height:1.5;
                      white-space:pre; overflow:auto; max-height:46vh; }
.ap-diff .d-add { color:var(--ok); }
.ap-diff .d-del { color:var(--err); }
.ap-diff .d-meta { color:var(--faint); }
.ap-foot { display:flex; align-items:center; gap:8px; }
/* 工具调用卡片的样式（.tgroups / .tgroup / .trow，JS 在 script 里生成） */
.tgroups { margin:0 0 8px; }
.tgroup { border:1px solid var(--line-soft); border-radius:10px;
          background:var(--elev); margin:4px 0; overflow:hidden; }
.tgroup-head { display:flex; align-items:center; gap:7px; padding:6px 10px;
               cursor:pointer; font-size:12.5px; color:var(--dim); }
.tgroup-head:hover { background:var(--hover); }
.tg-caret { color:var(--faint); font-size:10px; transition:transform .15s; }
.tgroup.open .tg-caret { transform:rotate(90deg); }
.tg-title { color:var(--fg); }
.tg-bad { color:var(--err); font-size:11.5px; }
.tgroup-head .sp { flex:1; }
.tgroup-body { display:none; padding:2px 10px 8px; }
.tgroup.open .tgroup-body { display:block; }
.trow { display:flex; align-items:baseline; gap:8px; padding:3px 0;
        font-size:12px; line-height:1.6; }
.trow + .trow { border-top:1px dashed var(--line-soft); }
.tverb { flex:none; color:var(--faint); font-size:11px; min-width:26px; }
.tfile { color:var(--accent); font-family:Consolas,monospace; flex:none; }
.tdir { color:var(--faint); font-size:11px; flex:1; min-width:0;
        overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.tplus { color:var(--ok); font-size:11.5px; flex:none; }
.tminus { color:var(--err); font-size:11.5px; flex:none; }
.tbadge { flex:none; color:var(--err); font-size:11px; }
.tcmd { color:var(--fg); font-family:Consolas,monospace; font-size:11.5px;
        flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;
        white-space:nowrap; }
.trow.bad .tfile, .trow.bad .tcmd { color:var(--err); }
/* Markdown 正文容器：关闭 pre-wrap——md() 输出的 HTML 源码里带换行，
   pre-wrap 会把它们显示成空行，与段落 margin 叠加成「间距过长」。
   代码块 .fence 自己是 white-space:pre，不受影响 */
.msg.bot .md-body { white-space:normal; }
/* 紧凑排版：浏览器给 <p> 的默认 1em 上下边距在 1.65 行高下会放大成大空隙，
   统一收成单向 margin-bottom（段落间 6px，末段不留尾空隙） */
.msg.bot p { margin:0 0 6px; }
.msg.bot p:last-child { margin-bottom:0; }
.msg.bot ul, .msg.bot ol { margin:3px 0 6px; padding-left:22px; }
.msg.bot li { margin:2px 0; }
.msg.bot li:last-child { margin-bottom:0; }
.msg.bot blockquote { margin:6px 0; padding:4px 12px; border-left:3px solid var(--accent);
                      background:var(--accent-soft); border-radius:0 8px 8px 0;
                      color:var(--dim); }
.msg.bot hr { border:none; border-top:1px solid var(--line-soft); margin:10px 0; }
.msg.bot em { font-style:italic; }
.msg.bot a { color:var(--accent); text-decoration:none; }
.msg.bot a:hover { text-decoration:underline; }
.msg.bot .fence { background:var(--bg); border:1px solid var(--line-soft);
                  border-radius:10px; padding:10px 12px; margin:6px 0;
                  font-family:Consolas,monospace; font-size:12.5px; line-height:1.6;
                  overflow-x:auto; white-space:pre; color:var(--fg);
                  user-select:text; -webkit-user-select:text; }
.msg.error { color:var(--err); }
.meta { color:var(--faint); font-size:11px; margin-top:7px; letter-spacing:.01em; }
.msg.bot .meta { padding-top:5px; border-top:1px dashed var(--line-soft); }
.meta .toolchip { background:var(--elev); border:1px solid var(--line); border-radius:999px;
                  padding:2px 9px; margin-right:5px; display:inline-block; font-size:11px; }
/* 思考块：参考「深度思考 ›」的轻量一行样式（无卡片边框，悬停变亮） */
.reasoning { margin:0 0 8px; }
.r-head { display:inline-flex; align-items:center; gap:6px; padding:2px 4px 2px 0;
          color:var(--faint); font-size:12.5px; cursor:pointer; user-select:none;
          border-radius:8px; transition:color .12s; }
.r-head:hover { color:var(--accent); }
.r-caret-end { font-size:13px; transition:transform .18s; }
.reasoning.open .r-caret-end { transform:rotate(90deg); }
.r-title { }
.r-body { display:none; margin:6px 0 2px; padding:2px 0 2px 12px; color:var(--dim);
          font-size:12px; line-height:1.7; white-space:pre-wrap;
          border-left:2px solid var(--line-soft); }
.reasoning.open .r-body { display:block; animation:rIn .18s ease; }
@keyframes rIn { from { opacity:0; transform:translateY(-3px); }
                 to { opacity:1; transform:none; } }
.thinking { display:inline-flex; align-items:center; gap:6px; padding:6px 0; }
.thinking i { width:6px; height:6px; border-radius:50%; background:var(--accent);
              animation:bounce 1.15s infinite ease-in-out; }
.thinking i:nth-child(2) { animation-delay:.15s; }
.thinking i:nth-child(3) { animation-delay:.3s; }
@keyframes bounce { 0%, 60%, 100% { transform:translateY(0); opacity:.3; }
                    30% { transform:translateY(-4px); opacity:1; } }

/* ---------- 输入卡片 ---------- */
.composer-wrap { padding:10px 24px 6px; }
.ws-bar { max-width:760px; margin:0 auto 8px; position:relative; display:none; }
#wsBtn { display:inline-flex; align-items:center; gap:7px; border:1px solid var(--line);
         background:var(--elev); color:var(--dim); border-radius:999px;
         padding:5px 14px; font-size:12.5px; cursor:pointer; max-width:340px; }
#wsBtn:hover { color:var(--fg); border-color:#3d3d3d; }
#wsBtn .ws-cur { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ws-bar .ws-close { border:none; background:var(--elev); color:var(--faint);
                    border-radius:999px; cursor:pointer; font-size:11px;
                    padding:6px 10px; margin-left:6px; }
.ws-bar .ws-close:hover { color:var(--fg); background:var(--hover); }
#wsMenu { display:none; position:absolute; top:36px; left:0; z-index:6; min-width:260px;
          max-width:360px; background:var(--panel); border:1px solid var(--line);
          border-radius:12px; padding:6px; box-shadow:0 8px 28px var(--shadow);
          animation:pop .15s ease; }
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
.composer { max-width:760px; margin:0 auto; background:var(--panel);
            border:1px solid var(--line-soft); border-radius:18px; padding:12px 14px 10px;
            transition:border-color .15s, box-shadow .2s; position:relative;
            box-shadow:0 8px 30px var(--shadow); }
.composer:focus-within { border-color:var(--accent); box-shadow:var(--ring), 0 8px 30px var(--shadow); }
#input { width:100%; background:transparent; border:none; outline:none; resize:none;
         color:var(--fg); font-size:14px; line-height:1.6; font-family:inherit;
         min-height:24px; max-height:180px; }
#input::placeholder { color:var(--faint); }
/* 窄宽度保护：拖窄侧栏/面板时输入行禁止换行挤压——按钮永不折行，
   只有权限徽标与模型选择允许收缩（省略号），避免「竖排文字」 */
.composer-row { display:flex; align-items:center; gap:7px; margin-top:10px;
                flex-wrap:nowrap; }
.composer-row > * { flex:none; }
/* @文件引用补全弹窗（锚定在 composer 内） */
#atMenu { display:none; position:absolute; left:14px; right:14px; bottom:calc(100% + 6px);
          background:var(--elev); border:1px solid var(--line); border-radius:12px;
          box-shadow:var(--shadow-lg); z-index:25; overflow:hidden; }
.at-item { padding:7px 12px; font-size:12.5px; color:var(--fg); cursor:pointer;
           font-family:Consolas,monospace; white-space:nowrap;
           overflow:hidden; text-overflow:ellipsis; }
.at-item.active, .at-item:hover { background:var(--accent-soft); color:var(--accent); }
/* 代码块工具条（复制 / 应用到文件） */
.fence-wrap { margin:8px 0; border:1px solid var(--line); border-radius:10px;
              overflow:hidden; background:var(--bg); }
.msg.bot .fence-wrap .fence { margin:0; border:none; border-radius:0; }
.fence-bar { display:flex; align-items:center; gap:6px; padding:4px 8px;
             background:var(--elev); border-bottom:1px solid var(--line-soft); }
.fence-lang { color:var(--accent); font-size:11px; font-family:Consolas,monospace;
              font-weight:600; letter-spacing:.03em; }
.flex1 { flex:1; }
.fence-btn { border:1px solid var(--line); background:transparent; color:var(--dim);
             border-radius:7px; padding:2px 9px; font-size:11px; cursor:pointer; }
.fence-btn:hover { color:var(--accent); border-color:var(--accent); }
/* 知识库试检索 */
.kbq-out { margin:8px 10px; padding:8px 10px; background:var(--elev);
           border:1px solid var(--line-soft); border-radius:10px;
           font-size:12px; color:var(--fg); white-space:pre-wrap;
           max-height:200px; overflow-y:auto; font-family:Consolas,monospace; }
/* 定时任务操作行 */
.sched-acts { display:flex; gap:8px; padding:6px 10px 8px; }
/* 多模型并答对比卡片 */
.cmp-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:4px 0; }
@media (max-width: 980px) { .cmp-grid { grid-template-columns:1fr; } }
.cmp-pane { border:1px solid var(--line); border-radius:12px; overflow:hidden;
            background:var(--panel); }
.cmp-head { padding:6px 12px; background:var(--elev); font-size:12px;
            color:var(--accent); font-weight:600;
            border-bottom:1px solid var(--line-soft); }
.cmp-body { padding:8px 12px; font-size:13.5px; line-height:1.6;
            white-space:pre-wrap; word-break:break-word; }
.cmp-err { color:var(--err); }
/* 统一控件外观：等高胶囊、elev 底、悬停上浮—— badges / 图标按钮 / 下拉 */
.badge, #ctxBtn, #imgBtn, #sideBtn, #snipBtn, #planBtn {
  background:var(--elev); border:1px solid var(--line-soft); color:var(--dim);
  border-radius:999px; padding:5px 12px; height:28px; font-size:11.5px;
  cursor:pointer; white-space:nowrap;
  transition:color .15s, border-color .15s, background .15s, transform .12s; }
.badge:hover, #ctxBtn:hover, #imgBtn:hover, #sideBtn:hover, #snipBtn:hover,
#planBtn:hover { color:var(--fg); border-color:var(--line); transform:translateY(-1px); }
/* 权限徽标与模型选择允许收缩（收缩时省略号），其余按钮永不压缩 */
#permBadge { flex:0 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; }
.badge.allow { color:var(--ok); border-color:rgba(92,198,137,.45); }
.badge.deny  { color:var(--err); border-color:rgba(239,123,109,.45); }
#ctxBtn:hover, #imgBtn:hover, #snipBtn:hover { color:var(--accent); }
#planBtn.on { color:var(--accent); border-color:var(--accent);
              background:var(--accent-soft); }
#sideBtn.off { color:var(--faint); }
/* 下拉：自定义箭头 + 胶囊外观（与按钮统一） */
#modelSel, #thinkSel { flex:0 1 auto; min-width:70px; max-width:170px;
            appearance:none; -webkit-appearance:none;
            background:var(--elev)
              url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='8' height='5'><path d='M1 1l3 3 3-3' stroke='%23888780' fill='none' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/></svg>")
              no-repeat right 10px center;
            border:1px solid var(--line-soft); color:var(--dim);
            border-radius:999px; padding:5px 24px 5px 12px; height:28px;
            font-size:11.5px; outline:none; cursor:pointer;
            transition:color .15s, border-color .15s; }
#modelSel:hover, #thinkSel:hover { color:var(--fg); border-color:var(--line); }
#modelSel:focus, #thinkSel:focus { color:var(--fg); border-color:var(--accent);
                                   box-shadow:var(--ring); }
#modelSel option, #thinkSel option { background:var(--elev); color:var(--fg); }
/* 左侧栏隐藏：只藏左侧 aside（右面板是 aside.right，不受影响），拖拽手柄一并隐藏 */
body.side-hidden .app > aside:not(.right) { display:none; }
body.side-hidden .side-resize { display:none; }
.img-chip { display:inline-flex; align-items:center; gap:6px; max-width:250px;
            background:var(--accent-soft); border:1px solid rgba(91,124,250,.28);
            color:var(--fg); border-radius:999px; padding:3px 10px; font-size:11.5px;
            white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
html[data-theme="light"] .img-chip { border-color:rgba(77,107,254,.24); }
.flex1 { flex:1; }
.send { width:34px; height:34px; border-radius:50%; border:none; cursor:pointer;
        background:var(--accent-grad); color:#fff; font-size:15px; line-height:1;
        display:flex; align-items:center; justify-content:center;
        box-shadow:var(--accent-glow);
        transition:filter .15s, transform .1s; }
.send:hover { filter:brightness(1.12); }
.send:active { transform:scale(.94); }
.send:disabled { opacity:.4; cursor:default; filter:none; box-shadow:none; }
.send.busy { background:transparent; border:2px solid var(--line);
             border-top-color:var(--accent); animation:spin .8s linear infinite;
             color:transparent; box-shadow:none; }
.send.stop { background:var(--panel); border:2px solid var(--err);
             color:var(--err); box-shadow:none; font-size:11px; }
.send.stop:hover { background:var(--err); color:#fff; filter:none; }
@keyframes spin { to { transform:rotate(360deg); } }
#usageLine { display:table; margin:7px auto 0; background:var(--elev);
             border:1px solid var(--line-soft); border-radius:999px;
             padding:4px 16px; color:var(--faint); font-size:11px;
             letter-spacing:.02em; max-width:90%; overflow:hidden;
             text-overflow:ellipsis; white-space:nowrap; }
#usageLine:empty { display:none; }
.composer-foot { padding:2px 24px 12px; }

/* ---------- 弹出菜单 / 卡片 ---------- */
.perm-menu { display:none; position:absolute; left:14px; bottom:52px; z-index:5;
             background:var(--panel); border:1px solid var(--line); border-radius:12px;
             padding:6px; min-width:210px; box-shadow:0 8px 28px rgba(0,0,0,.5);
             animation:pop .15s ease; }
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
.ctx-bar i { display:block; height:100%; background:var(--accent-grad); border-radius:3px; }
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
.step.pending .s-text { color:var(--accent); }
.step.result { color:var(--faint); font-size:11.5px; padding-left:22px; }
.stream-body { white-space:pre-wrap; word-break:break-word; min-height:1.2em; }
/* 流式生成中的光标：跟随 Markdown 内容末尾闪烁 */
.stream-body.live::after { content:'▍'; color:var(--accent);
                           animation:cursorBlink 1s steps(2, start) infinite; }
@keyframes cursorBlink { to { visibility:hidden; } }
/* 流式期间实时渲染的 Markdown 与最终版共用 .msg.bot 的排版规则 */
.stream-body p { margin:0 0 8px; }
.stream-body p:last-child { margin-bottom:0; }
.stream-body ul, .stream-body ol { margin:4px 0; }

/* ---------- 右侧面板 ---------- */
aside.right { width:360px; min-width:360px; background:var(--panel);
              border-left:1px solid var(--line-soft); display:flex;
              flex-direction:column; position:relative; }
#tabMenu { display:none; position:fixed; z-index:60;
           min-width:150px; background:var(--panel); border:1px solid var(--line);
           border-radius:12px; padding:5px; box-shadow:0 8px 28px var(--shadow);
           animation:pop .16s ease; }
#tabMenu.open { display:block; }
#tabMenu button { display:flex; align-items:center; gap:7px; width:100%; padding:7px 12px;
                  border:none; background:transparent; color:var(--dim); font-size:12.5px;
                  border-radius:8px; cursor:pointer; text-align:left; }
#tabMenu button:hover { background:var(--accent-soft); color:var(--accent); }
@keyframes pop { from { opacity:0; transform:translateY(-4px) scale(.98); }
                 to { opacity:1; transform:none; } }
.rp-resize { flex:none; width:5px; cursor:col-resize; background:transparent;
             transition:background .15s; }
.rp-resize:hover, .rp-resize.dragging { background:var(--accent-soft); }
.side-resize { flex:none; width:5px; cursor:col-resize; background:transparent;
               transition:background .15s; }
.side-resize:hover, .side-resize.dragging { background:var(--accent-soft); }
body.panel-hidden .rp-resize { display:none; }
body.panel-hidden aside.right { display:none; }
.rp-tabs { display:flex; align-items:center; gap:2px; padding:8px 8px;
           border-bottom:1px solid var(--line-soft);
           overflow-x:auto; scrollbar-width:none; }
.rp-tabs::-webkit-scrollbar { display:none; }
.rp-tab { border:none; background:transparent; color:var(--dim); font-size:12px;
          padding:5px 9px; border-radius:8px; cursor:pointer; user-select:none;
          transition:color .12s, background .12s;
          flex:none; white-space:nowrap; }
.rp-tab:hover { color:var(--fg); background:var(--hover); }
.rp-tab.active { background:var(--accent-soft); color:var(--accent); font-weight:600; }
.rp-tabs .rp-close { margin-left:0; background:transparent; border:none;
                     color:var(--faint); font-size:13px; cursor:pointer;
                     padding:3px 8px; border-radius:8px; flex:none; }
.rp-tabs .rp-close:hover { background:var(--hover); color:var(--fg); }
.rp-tab { position:relative; }
.rp-tab .tab-x { visibility:hidden; margin-left:5px; color:var(--faint);
                 font-size:10px; padding:0 1px; }
.rp-tab:hover .tab-x { visibility:visible; }
.rp-tab .tab-x:hover { color:var(--err); }
.rp-plus { margin-left:auto; color:var(--faint); font-size:14px; padding:5px 10px; }
.rp-plus:hover { color:var(--accent); }
.rp-plus.dim { opacity:.35; cursor:default; }
.rp-plus.dim:hover { color:var(--faint); }
.rp-body { flex:1; min-height:0; display:none; flex-direction:column; }
.rp-body.active { display:flex; }
.rp-pane { flex:1; overflow-y:auto; padding:12px; font-size:13px; }
.side-action.off { color:var(--faint); }

/* 工作区树 */
.ws-nav { display:flex; align-items:center; gap:6px; padding:8px 12px;
          border-bottom:1px solid var(--line-soft); }
.ws-nav .crumb { flex:1; color:var(--dim); font-size:12px; overflow:hidden;
                 text-overflow:ellipsis; white-space:nowrap; direction:rtl;
                 text-align:left; }
.mini-btn { border:1px solid var(--line); background:transparent; color:var(--dim);
            border-radius:7px; font-size:11.5px; padding:3px 9px; cursor:pointer;
            transition:color .12s, border-color .12s, background .12s; }
.mini-btn:hover { color:var(--fg); border-color:#3d3d3d; }
/* ---------- 文件预览（工作区面板内） ---------- */
.fp { display:none; border-top:1px solid var(--line-soft); padding:10px 12px;
      overflow:auto; max-height:52%; }
.fp.open { display:block; }
.fp-head { display:flex; align-items:center; gap:8px; margin-bottom:8px;
           flex-wrap:wrap; }
.fp-name { font-weight:600; font-size:12.5px; color:var(--fg); word-break:break-all; }
.fp-meta { color:var(--faint); font-size:11px; }
.fp-head .sp { flex:1; }
.fp-img { max-width:100%; max-height:320px; border-radius:8px;
          border:1px solid var(--line-soft); display:block; }
/* 内嵌渲染：PDF / HTML 用 iframe，音视频用原生播放器 */
.fp-frame { width:100%; height:360px; border:1px solid var(--line-soft);
            border-radius:8px; background:#fff; }
.fp-media { width:100%; max-height:300px; border-radius:8px;
            border:1px solid var(--line-soft); background:var(--bg); display:block; }
.fp-html-wrap { display:flex; flex-direction:column; gap:6px; }
.fp-html-bar { display:flex; justify-content:flex-end; }
.fp-html-view { min-height:0; }
.fp-code { white-space:pre-wrap; word-break:break-word; font-size:11.5px;
           line-height:1.6; max-height:320px; overflow:auto;
           background:var(--bg); border:1px solid var(--line-soft);
           border-radius:8px; padding:10px 12px; }
.c-com { color:var(--faint); font-style:italic; }
.c-str { color:var(--ok); }
.c-num { color:var(--accent); }
.c-kw { color:var(--accent); font-weight:600; }
.fp-table { overflow:auto; max-height:300px; border:1px solid var(--line-soft);
            border-radius:8px; }
.fp-table table { border-collapse:collapse; font-size:11.5px; width:100%; }
.fp-table td, .fp-table th { border-bottom:1px solid var(--line-soft);
                             padding:4px 8px; white-space:nowrap; }
.fp-table th { background:var(--elev); position:sticky; top:0; }
.fp-info { color:var(--dim); font-size:12px; line-height:1.7; }

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
#browserFrame { flex:1; border:none; background:var(--bg); width:100%; display:none; }
#browserFrame.on { display:block; }
.br-empty { position:absolute; inset:0; display:flex; flex-direction:column;
             align-items:center; justify-content:center; gap:8px; color:var(--faint);
             font-size:12.5px; text-align:center; padding:0 20px; }
.br-empty .br-glyph { font-size:34px; opacity:.55; }

/* 浏览器地址栏在标签条下方，去掉原顶边距 */
#rp-browser .br-bar { border-top:1px solid var(--line-soft); }

/* 审查 */
.review-out { flex:1; overflow-y:auto; background:var(--bg); margin:0; padding:12px;
              font-family:Consolas,monospace; font-size:12px; line-height:1.6;
              color:var(--dim); white-space:pre-wrap; word-break:break-all;
              user-select:text; -webkit-user-select:text; }
.diff-add { color:var(--ok); }
.diff-del { color:var(--err); }
/* 远程仓库配置行（审查面板，用户手动提交/推送的入口） */
.git-remote { display:flex; gap:6px; padding:8px 12px;
              border-bottom:1px solid var(--line-soft); }
.git-remote input { flex:1; min-width:0; background:var(--bg); border:1px solid var(--line);
                    border-radius:8px; color:var(--fg); padding:6px 10px;
                    font-size:11.5px; outline:none; }
.git-remote input:focus { border-color:var(--accent); }

/* 辅助对话 */
#auxThread { flex:1; overflow-y:auto; padding:12px; }
#auxThread .msg { font-size:13px; margin-bottom:12px; }
#auxThread .msg.user { font-size:13px; padding:8px 13px; }
.aux-top { display:flex; align-items:center; gap:8px; padding:8px 12px 0; }
.aux-top select { flex:1; min-width:0; background:var(--elev); color:var(--fg);
                  border:1px solid var(--line); border-radius:8px;
                  padding:4px 8px; font-size:12px; outline:none; }
.aux-row { display:flex; align-items:center; gap:8px; padding:8px 12px;
           border-top:1px solid var(--line-soft); }
.aux-row input { flex:1; min-width:0; background:var(--bg); border:1px solid var(--line);
                 border-radius:10px; color:var(--fg); padding:8px 12px;
                 font-size:12.5px; outline:none; }
.aux-row .send { flex:none; width:34px; height:34px; min-width:34px;
                 min-height:34px; font-size:14px; }
.aux-hint { color:var(--faint); font-size:10.5px; padding:0 12px 8px; }

/* ---------- 对话框 ---------- */
#dlgOverlay { position:fixed; inset:0; background:rgba(6,8,18,.45); display:none;
               align-items:center; justify-content:center; z-index:20;
               backdrop-filter:blur(6px); }
#dlg { background:var(--panel); border:1px solid var(--line-soft); border-radius:18px;
       width:min(440px, 90vw); padding:20px; box-shadow:var(--shadow-lg);
       animation:pop .18s ease; }
#dlg h3 { margin:0 0 10px; font-size:15px; }
#dlg .msg-text { color:var(--dim); font-size:13px; line-height:1.7; white-space:pre-wrap; }
#dlg input { width:100%; margin-top:12px; background:var(--bg); border:1px solid var(--line);
             border-radius:10px; color:var(--fg); padding:9px 12px; font-size:13px; outline:none;
             transition:border-color .15s, box-shadow .15s; }
#dlg input:focus { border-color:var(--accent); box-shadow:var(--ring); }
#dlgChoose { display:none; margin-top:10px; max-height:300px; overflow-y:auto; }
#dlgChoose .choose-item { display:flex; align-items:baseline; gap:8px; width:100%;
                          padding:9px 12px; margin:2px 0; border:1px solid transparent;
                          background:transparent; color:var(--dim); font-size:13px;
                          border-radius:10px; cursor:pointer; text-align:left; }
#dlgChoose .choose-item:hover { background:var(--accent-soft); color:var(--accent); }
#dlgChoose .choose-label { flex:none; }
#dlgChoose .choose-sub { flex:1; overflow:hidden; text-overflow:ellipsis;
                         white-space:nowrap; color:var(--faint); font-size:11px;
                         direction:rtl; text-align:left; }
#dlg .dlg-row { display:flex; justify-content:flex-end; gap:10px; margin-top:16px; }

/* ---------- 设置弹窗 ---------- */
#overlay { position:fixed; inset:0; background:rgba(6,8,18,.45); display:none;
           align-items:center; justify-content:center; z-index:10;
           backdrop-filter:blur(6px); }
#modal { background:var(--panel); border:1px solid var(--line-soft); border-radius:18px;
         width:min(760px, 92vw); max-height:86vh; display:flex; flex-direction:column;
         overflow:hidden; box-shadow:var(--shadow-lg); }
.tabs { display:flex; align-items:center; gap:4px; padding:12px 16px;
        border-bottom:1px solid var(--line-soft); }
.tab { border:none; background:transparent; color:var(--dim); font-size:13px;
       padding:6px 14px; border-radius:8px; cursor:pointer; transition:color .12s; }
.tab:hover { color:var(--fg); }
.tab.active { background:var(--accent-soft); color:var(--accent); font-weight:600; }
.tabs .close { margin-left:auto; background:transparent; border:none; color:var(--dim);
               font-size:15px; cursor:pointer; padding:4px 8px; border-radius:8px; }
.tabs .close:hover { background:var(--hover); color:var(--fg); }
.tab-body { padding:16px; overflow-y:auto; }
.hint { color:var(--faint); font-size:12px; line-height:1.6; }
.note-ok { color:var(--ok); font-size:12px; }
.note-err { color:var(--err); font-size:12px; }
.model-card { background:var(--elev); border:1px solid var(--line-soft); border-radius:12px;
              padding:12px; margin:10px 0; transition:border-color .15s, box-shadow .15s; }
.model-card:hover { border-color:var(--line); box-shadow:0 4px 14px var(--shadow); }
.model-presets { display:flex; align-items:center; gap:8px; margin-bottom:10px;
                 padding-bottom:10px; border-bottom:1px dashed var(--line-soft); }
.model-presets label { color:var(--faint); font-size:11.5px; flex:none; }
.model-presets select { flex:1; max-width:420px; background:var(--bg);
                        border:1px solid var(--line); border-radius:8px;
                        color:var(--dim); padding:5px 8px; font-size:12px; outline:none; }
.model-presets select:focus { border-color:var(--accent); color:var(--fg); }
.model-grid { display:grid; grid-template-columns:1fr 120px 1fr; gap:8px; }
.model-grid2 { display:grid; grid-template-columns:1fr 1fr auto auto; gap:8px; margin-top:8px;
               align-items:end; }
.field input, .field select { width:100%; background:var(--bg); border:1px solid var(--line);
         border-radius:8px; color:var(--fg); padding:7px 10px; font-size:12.5px; outline:none;
         transition:border-color .15s, box-shadow .15s; }
.field input:focus, .field select:focus { border-color:var(--accent); box-shadow:var(--ring); }
.field label { display:block; color:var(--faint); font-size:10.5px; margin-bottom:4px;
               letter-spacing:.06em; }
.radio-default { display:flex; align-items:center; gap:6px; color:var(--dim);
                 font-size:12px; cursor:pointer; padding:0 4px 8px; }
.radio-default input { accent-color: var(--accent); }
.icon-btn { background:transparent; border:1px solid var(--line); color:var(--dim);
            border-radius:8px; padding:6px 12px; font-size:12px; cursor:pointer; }
.icon-btn:hover { color:var(--err); border-color:rgba(239,123,109,.4); }
.modal-footer { display:flex; align-items:center; gap:10px; margin-top:14px; }
.primary { background:var(--accent-grad); border:none; color:#fff; border-radius:10px;
           padding:8px 22px; font-size:13px; font-weight:600; cursor:pointer;
           box-shadow:var(--accent-glow); transition:filter .15s; }
.primary:hover { filter:brightness(1.1); }
.ghost { background:transparent; border:1px solid var(--line); color:var(--dim);
         border-radius:10px; padding:8px 18px; font-size:13px; cursor:pointer; }
.ghost:hover { color:var(--fg); border-color:var(--accent); }
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
.market-row .m-install button { background:var(--accent-grad); border:none; color:#fff;
                                border-radius:8px; padding:5px 14px; font-size:12px;
                                cursor:pointer; font-weight:600;
                                box-shadow:var(--accent-glow); transition:filter .15s; }
.market-row .m-install button:hover { filter:brightness(1.1); }
.market-row .m-install button:disabled { opacity:.45; cursor:default; }

/* ---------- 设置弹窗：左导航 + 右内容（对齐 WorkBuddy 风格） ---------- */
/* 固定高度：所有设置页统一尺寸，切换标签时弹窗不再跳变；内容区自行滚动 */
#modal.settings { width:min(880px, 94vw); height:min(720px, 88vh); max-height:88vh;
                  flex-direction:row; position:relative; padding:0; }
.set-close { position:absolute; top:10px; right:12px; z-index:2; border:none;
             background:transparent; color:var(--dim); font-size:15px; cursor:pointer;
             padding:6px 9px; border-radius:8px; }
.set-close:hover { background:var(--hover); color:var(--fg); }
.set-nav { width:176px; flex:none; border-right:1px solid var(--line-soft);
           padding:14px 10px; overflow-y:auto; }
.set-group { color:var(--faint); font-size:10.5px; letter-spacing:.08em;
             padding:14px 12px 5px; }
.set-group:first-child { padding-top:2px; }
.set-nav-item { display:flex; align-items:center; gap:9px; width:100%; height:34px;
                padding:0 12px; border:none; background:transparent; color:var(--dim);
                font-size:13px; border-radius:10px; cursor:pointer; text-align:left;
                transition:color .12s, background .12s; }
.set-nav-item .ic { width:19px; text-align:center; flex:none; font-size:13px; }
.set-nav-item:hover { background:var(--hover); color:var(--fg); }
.set-nav-item.active { background:var(--accent-soft); color:var(--accent); }
.set-content { flex:1; min-width:0; overflow-y:auto; padding:20px 22px; }
.set-title { font-size:15px; font-weight:500; color:var(--fg); margin:0 0 10px; }
.set-card { background:var(--elev); border:1px solid var(--line-soft);
            border-radius:12px; padding:4px 16px; }
.set-main { flex:1; min-width:0; }
.set-main .name { font-size:13px; color:var(--fg); }
.set-main .desc { font-size:11.5px; color:var(--faint); margin-top:2px; line-height:1.5; }

/* 分段按钮 / 色板 / 开关行 */
.set-h { color:var(--faint); font-size:11px; letter-spacing:.08em; margin:18px 0 8px; }
.seg { display:flex; gap:8px; flex-wrap:wrap; }
.seg button { border:1px solid var(--line); background:transparent; color:var(--dim);
              border-radius:10px; padding:7px 14px; font-size:12.5px; cursor:pointer;
              transition:color .12s, border-color .12s, background .12s; }
.seg button:hover { color:var(--fg); }
.seg button.sel { color:var(--accent); border-color:var(--accent); background:var(--accent-soft); }
.swatches { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:4px 0 10px; }
.swatch { width:34px; height:34px; border-radius:10px; cursor:pointer;
          border:2px solid transparent; padding:0; }
.swatch.sel { border-color:var(--fg); }
.swatch:hover { transform:translateY(-1px); }
.set-row { display:flex; align-items:center; gap:10px; padding:10px 0;
           border-bottom:1px solid var(--line-soft); }
.set-row:last-child { border-bottom:none; }
.data-actions { display:flex; gap:8px; flex-wrap:wrap; }
.stat-cards { display:flex; gap:10px; flex-wrap:wrap; margin:10px 0; }
.stat { flex:1; min-width:150px; background:var(--elev); border:1px solid var(--line-soft);
        border-radius:12px; padding:10px 14px; }
.stat .v { font-size:18px; font-weight:500; color:var(--fg); }
.stat .l { font-size:11.5px; color:var(--faint); margin-top:2px; }

/* ---------- 消息操作（复制 / 编辑 / 重新生成） ---------- */
.msg-acts { display:flex; gap:6px; margin-top:5px; opacity:0; transition:opacity .15s; }
.msg:hover .msg-acts { opacity:1; }
.msg-act { border:1px solid var(--line); background:var(--elev); color:var(--dim);
           border-radius:999px; font-size:11px; padding:2px 10px; cursor:pointer;
           transition:color .12s, border-color .12s; }
.msg-act:hover { color:var(--accent); border-color:var(--accent); }
/* 搜索命中跳转时的高亮 */
.msg.flash { animation:msgFlash 1.6s ease; border-radius:10px; }
@keyframes msgFlash { 0%,55% { background:var(--accent-soft); } 100% { background:transparent; } }

/* ---------- 会话置顶星标 ---------- */
.s-row .pin { border:none; background:transparent; color:var(--faint); cursor:pointer;
              font-size:12px; padding:0 2px; visibility:hidden; flex:none; }
.s-row:hover .pin, .s-row .pin.on { visibility:visible; }
.s-row .pin.on { color:var(--accent); }
.s-row .pin:hover { color:var(--accent); }
/* 会话完成标记（F8） */
.s-row .row-done { border:none; background:transparent; color:var(--faint); cursor:pointer;
                   font-size:12px; padding:0 2px; visibility:hidden; flex:none; }
.s-row:hover .row-done, .s-row .row-done.on { visibility:visible; }
.s-row .row-done.on { color:var(--ok); }
.s-row.done .s-name { color:var(--faint); text-decoration:line-through; }

/* ---------- 任务面板（TODO.md 渲染） ---------- */
.todo-row { display:flex; align-items:baseline; gap:8px; padding:6px 4px;
            border-bottom:1px solid var(--line-soft); font-size:12.5px; }
.t-box { flex:none; width:14px; height:14px; border:1.5px solid var(--dim);
         border-radius:4px; display:inline-flex; align-items:center;
         justify-content:center; font-size:10px; color:#fff; }
.t-doing-box { border-color:var(--accent); color:var(--accent); }
.t-done-box { background:var(--ok); border-color:var(--ok); }
.t-done .t-name { color:var(--faint); text-decoration:line-through; }
.t-doing .t-name { color:var(--accent); }
.todo-hint { color:var(--faint); font-size:11px; padding:2px 4px; white-space:pre-wrap; }

/* ---------- 用量图表 ---------- */
.chart-box { padding:10px 4px; }
.chart-box svg { width:100%; height:auto; display:block; }
.chart-legend { display:flex; gap:14px; align-items:center; color:var(--dim);
                font-size:11.5px; margin:6px 4px; }
.chart-legend i { display:inline-block; width:10px; height:3px; border-radius:2px;
                  margin-right:4px; vertical-align:middle; background:var(--accent); }

/* ---------- 浏览器标签多开 ---------- */
.br-tabs { display:flex; align-items:center; gap:4px; padding:6px 10px 0;
           overflow-x:auto; scrollbar-width:none; }
.br-tabs::-webkit-scrollbar { display:none; }
.br-tab { display:flex; align-items:center; gap:6px; border:1px solid var(--line-soft);
          background:var(--elev); color:var(--dim); font-size:11.5px; padding:4px 10px;
          border-radius:8px 8px 0 0; cursor:pointer; max-width:150px; flex:none; }
.br-tab.active { color:var(--fg); border-color:var(--line); background:var(--panel); }
.br-tab .bt-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.br-tab .bt-x { color:var(--faint); padding:0 1px; }
.br-tab .bt-x:hover { color:var(--err); }
.br-add { border:none; background:transparent; color:var(--dim); font-size:13px;
          cursor:pointer; flex:none; padding:2px 8px; }
.br-add:hover { color:var(--accent); }
.br-frames { flex:1; position:relative; min-height:0; }
.br-frames iframe { position:absolute; inset:0; width:100%; height:100%; border:none;
                    background:var(--bg); display:none; }
.br-frames iframe.on { display:block; }

/* ---------- 图片预览（点击 chips 放大） ---------- */
#lightbox { position:fixed; inset:0; display:none; align-items:center; justify-content:center;
            background:rgba(6,8,18,.72); z-index:40; cursor:zoom-out; }
#lightbox.on { display:flex; }
#lightbox img { max-width:92%; max-height:92%; border-radius:12px;
                box-shadow:var(--shadow-lg); background:var(--panel); }
/* 键盘按键样式（通用页快捷键说明） */
.kbd { border:1px solid var(--line); background:var(--bg); color:var(--dim);
       border-radius:6px; padding:2px 8px; font-size:11px;
       font-family:Consolas,monospace; white-space:nowrap; }

/* ---------- 轻提示（toast）：操作反馈不再写进消息会话 ---------- */
#toast { position:fixed; top:14px; left:50%; transform:translateX(-50%) translateY(-8px);
         z-index:50; display:flex; flex-direction:column; gap:6px; align-items:center;
         pointer-events:none; opacity:0; transition:opacity .18s, transform .18s; }
#toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
#toast .t-item { background:var(--panel); border:1px solid var(--line);
                 border-left:3px solid var(--accent); color:var(--fg);
                 border-radius:10px; padding:8px 16px; font-size:12.5px;
                 box-shadow:var(--shadow-lg); max-width:70vw;
                 overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#toast .t-item.err { border-left-color:var(--err); }

/* ---------- 拖拽文件入窗 ---------- */
#dropMask { position:fixed; inset:0; display:none; align-items:center; justify-content:center;
            background:rgba(6,8,18,.5); z-index:30; pointer-events:none;
            color:#fff; font-size:15px; letter-spacing:.04em; }
#dropMask .dm-card { background:var(--panel); border:1px solid var(--accent);
                     border-radius:14px; padding:18px 30px; box-shadow:var(--shadow-lg); }
body.dragging #dropMask { display:flex; }
</style></head><body>
<div class="app">
  <aside>
    <div class="brand">
      <div class="logo">卅</div>
      <div><b>卅 HARNESS</b><small>LOCAL-FIRST AGENT</small></div>
    </div>
    <button class="new-btn" onclick="newSessionFlow()">＋ 新会话</button>
    <div class="side-label">项目</div>
    <div class="side-filter"><input id="sessionFilter" placeholder="🔍 过滤会话…" spellcheck="false"></div>
    <div id="wsTree" class="side-list"></div>
    <div class="side-bottom">
      <button class="side-action" onclick="openSettings()" title="外观 / 模型 / 插件 / 技能 / 用量">⚙ 设置</button>
      <div class="side-hint" id="statusHint"></div>
    </div>
  </aside>
  <div class="side-resize" id="sideResize" title="拖动调整侧栏宽度"></div>
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
        <button class="ws-close" onclick="dismissWsBar()" title="隐藏工作区选择（可在左侧「📁 工作区」找回）">✕</button>
        <div id="wsMenu"></div>
      </div>
      <div class="composer">
        <div id="atMenu"></div>
        <textarea id="input" rows="1"
          placeholder="描述你想做的事…（Enter 发送，Shift+Enter 换行，@ 可引用工作区文件）"></textarea>
        <div id="imgChips" style="display:none;flex-wrap:wrap;gap:6px;padding:0 10px;"></div>
        <div class="composer-row">
          <button id="sideBtn" title="显示/隐藏左侧栏" onclick="toggleSide()">◧ 侧栏</button>
          <button id="permBadge" class="badge" title="执行权限" onclick="togglePermMenu()"></button>
          <span class="flex1"></span>
          <button id="snipBtn" title="快捷指令（常用提示词片段，一键插入）" onclick="snipMenu()">⚡</button>
          <button id="imgBtn" title="附加图片（或直接粘贴图片路径）" onclick="attachImage()">📎</button>
          <button id="ctxBtn" title="上下文占用" onclick="toggleCtxCard()">…</button>
          <button id="planBtn" title="计划模式：实现类任务先出计划，确认后再动手" onclick="togglePlanMode()">📋 计划</button>
          <select id="thinkSel" title="思考级别（映射 reasoning_effort）"></select>
          <select id="modelSel" title="当前模型"></select>
          <span id="queueBadge" class="q-badge" style="display:none" title="本轮回答完成后将自动依次发送这些消息"></span>
          <button id="cmpBtn" title="与另一个模型并答对比（当前输入的问题，纯对话不带工具）" onclick="compareFlow()">⚖</button>
          <button id="stopBtn" class="send stop" style="display:none" title="停止生成（部分内容不保留）" onclick="stopStream()">■</button>
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
          <h5>GIT 提交</h5>
          <button data-kind="git" data-mode="ask" onclick="pickPerm('git','ask')">💬 每次询问</button>
          <button data-kind="git" data-mode="allow" onclick="pickPerm('git','allow')">✅ 允许提交</button>
          <button data-kind="git" data-mode="deny" onclick="pickPerm('git','deny')">🚫 禁止 git</button>
        </div>
        <div id="ctxCard"></div>
      </div>
    </div>
    <div class="composer-foot"><div id="usageLine"></div></div>
  </main>
  <div class="rp-resize" id="rpResize" title="拖动调整右侧面板宽度"></div>
  <aside class="right" id="rightPanel">
    <div class="rp-tabs">
      <span id="rpTabsBox" style="display:contents"></span>
      <button class="rp-close" onclick="togglePanel()" title="收起面板">✕</button>
      <div id="tabMenu" title=""></div>
    </div>

    <div class="rp-body active" id="rp-aux">
      <div class="aux-top">
        <span class="hint">模型</span>
        <select id="auxModelSel" title="辅助对话使用的模型（不影响主会话的当前模型）"></select>
      </div>
      <div id="auxThread"></div>
      <div class="aux-row">
        <input id="auxInput" placeholder="提出后续修改要求…（Enter 发送）">
        <button id="auxSend" class="send" onclick="auxSend()">↑</button>
      </div>
      <div class="aux-hint">独立辅助对话 —— 与左侧主会话互不影响，可单独选模型；共用工具与工作区。</div>
    </div>

    <div class="rp-body" id="rp-ws">
      <div class="ws-nav">
        <button class="mini-btn" onclick="wsUp()">⬆ 上级</button>
        <span class="crumb" id="wsCrumb">/</span>
        <button class="mini-btn" title="撤销上一次文件改动（写文件前自动留底）" onclick="undoLastChange()">↶ 撤销</button>
        <button class="mini-btn" onclick="loadWsTree()">⟳</button>
      </div>
      <div class="rp-pane" id="wsTreePane"></div>
      <div class="fp" id="filePreview"></div>
    </div>

    <div class="rp-body" id="rp-kb">
      <div class="rp-pane" id="kbPane"><div class="hint">加载中…</div></div>
      <div class="term-row kbq-row">
        <input id="kbqInput" placeholder="试检索：直接问知识库，看命中片段（不进对话）">
        <button class="mini-btn" onclick="kbQuery()">🔍</button>
      </div>
      <pre class="kbq-out" id="kbqOut" style="display:none"></pre>
      <div class="term-row">
        <button class="mini-btn" onclick="kbPickIndex()" title="选择一个文件夹，把其中的文本文件索引进知识库">📂 索引文件夹…</button>
        <button class="mini-btn" onclick="kbClear()">🗑 清空</button>
        <span class="flex1"></span>
        <button class="mini-btn" onclick="loadKnowledge()">⟳</button>
      </div>
    </div>

    <div class="rp-body" id="rp-todo">
      <div class="ws-nav">
        <span class="crumb">工作区 TODO.md（任务状态索引）</span>
        <button class="mini-btn" onclick="loadTodoPanel()">⟳</button>
      </div>
      <div class="rp-pane" id="todoPane"><div class="hint">切换到此标签页时自动加载。</div></div>
      <div class="term-row"><span class="hint" style="padding:0 12px">由 agent 的 todo_write 维护；你也可以直接编辑 TODO.md。</span></div>
    </div>

    <div class="rp-body" id="rp-sched">
      <div class="ws-nav">
        <span class="crumb">定时任务（scheduler 插件到点执行）</span>
        <button class="mini-btn" onclick="loadSchedules()">⟳</button>
      </div>
      <div class="rp-pane" id="schedPane"><div class="hint">切换到此标签页时自动加载。</div></div>
      <div class="term-row"><span class="hint" style="padding:0 12px">新建任务：sha schedule add &lt;名&gt; --every &lt;秒&gt; --prompt &lt;提示词&gt;，或直接让 agent 帮你创建。</span></div>
    </div>

    <div class="rp-body" id="rp-mem">
      <div class="ws-nav">
        <span class="crumb">长期记忆（每轮对话自动注入，跨会话生效）</span>
        <button class="mini-btn" onclick="loadMemory()">⟳</button>
      </div>
      <div class="rp-pane" style="display:flex;flex-direction:column;padding:8px 10px;min-height:0;">
        <textarea id="memText" style="flex:1;min-height:200px;resize:none;border:1px solid var(--line);border-radius:10px;background:var(--elev);color:var(--fg);padding:10px;font-size:12.5px;line-height:1.6;font-family:Consolas,monospace;"
          placeholder="写点让 agent 永远记住的事，例如：&#10;- 本项目用 pytest，测试命令：python -m pytest -q&#10;- 提交信息用中文，格式：类型: 摘要&#10;- 不要动 legacy/ 目录"></textarea>
        <div class="term-row" style="padding:8px 0 0">
          <span class="hint" style="padding:0 6px">保存到 profile/memory.md，下一轮对话生效。</span>
          <span class="flex1"></span>
          <button class="mini-btn" onclick="saveMemory()">💾 保存</button>
        </div>
      </div>
    </div>

    <div class="rp-body" id="rp-sub">
      <div class="ws-nav">
        <span class="crumb">子 agent 调用记录（最新在前）</span>
        <button class="mini-btn" onclick="clearSubagents()" title="清空全部子 agent 记录">🗑</button>
        <button class="mini-btn" onclick="loadSubagents()">⟳</button>
      </div>
      <div class="rp-pane" id="subPane"><div class="hint">切换到此标签页时自动加载。</div></div>
    </div>

    <div class="rp-body" id="rp-usage">
      <div class="ws-nav">
        <span class="crumb">Token 用量（近 30 天，按天聚合）</span>
        <button class="mini-btn" onclick="loadUsageChart()">⟳</button>
      </div>
      <div class="rp-pane" id="usagePane"><div class="hint">切换到此标签页时自动加载。</div></div>
    </div>

    <div class="rp-body" id="rp-term">
      <pre class="term-out" id="termOut">在此输入命令，在工作区目录执行。</pre>
      <div class="term-row">
        <input id="termInput" placeholder="命令，如 git status" spellcheck="false">
        <button class="mini-btn" onclick="termRun()">执行</button>
      </div>
    </div>

    <div class="rp-body" id="rp-browser">
      <div class="br-tabs" id="brTabs"></div>
      <div class="br-bar">
        <input id="brUrl" placeholder="输入网址，如 https://example.com">
        <button class="mini-btn" onclick="brGo()">打开</button>
        <button class="mini-btn" onclick="brExternal()">系统浏览器</button>
      </div>
      <div class="br-frames" id="brFrames">
        <div class="br-empty" id="brEmpty">
          <span class="br-glyph">🌐</span>
          <span>在上方输入网址后打开；部分站点（如 GitHub）禁止内嵌，请用「系统浏览器」。<br>支持多标签页：点「＋」新开一个。</span>
        </div>
      </div>
    </div>

    <div class="rp-body" id="rp-review">
      <div class="ws-nav">
        <span class="crumb">工作区改动（git status + diff）</span>
        <button class="mini-btn" onclick="loadReview()">⟳ 生成</button>
      </div>
      <div class="git-remote">
        <input id="gitRemoteUrl" placeholder="远程仓库地址（origin），如 https://github.com/user/repo.git">
        <button class="mini-btn" onclick="saveGitRemote()">保存</button>
        <button class="mini-btn" onclick="pushNow()">⇅ 推送</button>
      </div>
      <pre class="review-out" id="reviewOut">点击右上角「生成」查看当前工作区的未提交改动。</pre>
    </div>
  </aside>
</div>

<div id="dlgOverlay">
  <div id="dlg">
    <h3 id="dlgTitle"></h3>
    <div class="msg-text" id="dlgMsg"></div>
    <div id="dlgChoose" style="display:none"></div>
    <input id="dlgInput" style="display:none">
    <div class="dlg-row" id="dlgRow">
      <button class="ghost" id="dlgCancel">取消</button>
      <button class="primary" id="dlgOk">确定</button>
    </div>
  </div>
</div>

<div id="overlay">
  <div id="modal" class="settings">
    <button class="set-close" onclick="closeSettings()" title="关闭设置">✕</button>
    <div class="set-nav">
      <div class="set-group">能力</div>
      <button class="set-nav-item active" data-set="models" onclick="switchTab('models')"><span class="ic">🧩</span>模型</button>
      <button class="set-nav-item" data-set="plugins" onclick="switchTab('plugins')"><span class="ic">🔌</span>插件</button>
      <button class="set-nav-item" data-set="skills" onclick="switchTab('skills')"><span class="ic">📚</span>技能</button>
      <button class="set-nav-item" data-set="market" onclick="switchTab('market')"><span class="ic">🛒</span>市场</button>
      <button class="set-nav-item" data-set="mcp" onclick="switchTab('mcp')"><span class="ic">🔗</span>MCP</button>
      <div class="set-group">界面</div>
      <button class="set-nav-item" data-set="appearance" onclick="switchTab('appearance')"><span class="ic">🎨</span>外观</button>
      <button class="set-nav-item" data-set="general" onclick="switchTab('general')"><span class="ic">⚙</span>通用</button>
      <button class="set-nav-item" data-set="usage" onclick="switchTab('usage')"><span class="ic">📈</span>用量</button>
    </div>
    <div class="set-content">
      <div id="tab-models">
        <div class="set-title">模型</div>
        <div class="hint">至少填写 名称 / 模型 / API Key（或选「提供商预设」只填 Key）；provider 仅支持 openai / anthropic。保存后立即生效，无需重启。</div>
        <div id="cards"></div>
        <div class="modal-footer">
          <button class="ghost" onclick="addCard()">＋ 添加模型</button>
          <span class="flex1"></span>
          <span id="saveNote"></span>
          <button class="primary" onclick="saveSettings()">保存</button>
        </div>
      </div>
      <div id="tab-plugins" style="display:none">
        <div class="set-title">插件</div>
        <div class="hint">插件目录需含 plugin.json 与 register.py。输入本地路径即可安装；点击条目右侧移除。</div>
        <div id="pluginCards"></div>
        <div class="install-row">
          <input id="pluginPath" placeholder="本地插件目录路径，如 D:\plugins\my-plugin">
          <button class="ghost" onclick="installPlugin()">安装</button>
        </div>
        <div class="modal-footer"><span id="pluginNote"></span></div>
      </div>
      <div id="tab-skills" style="display:none">
        <div class="set-title">技能</div>
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
        <div class="set-title">市场</div>
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
      <div id="tab-mcp" style="display:none">
        <div class="set-title">MCP 服务器</div>
        <div class="hint">stdio 填启动命令（参数用空格分隔，含空格的路径建议写进命令行引号内或改用 http）；
          http / sse 填 URL。Headers 与 Env 用 JSON 对象（如 {"Authorization": "Bearer sk-..."}），
          值以掩码显示，保存时回传掩码即保持原值。保存后自动断开重连，下一轮对话生效。</div>
        <div id="mcpCards"></div>
        <div class="modal-footer">
          <button class="ghost" onclick="addMcpCard()">＋ 添加服务器</button>
          <span class="flex1"></span>
          <span id="mcpSaveNote"></span>
          <button class="primary" onclick="saveMcp()">保存并重连</button>
        </div>
      </div>
      <div id="tab-appearance" style="display:none">
        <div class="set-title">外观</div>
        <div class="hint">主题、强调色与字体，更改即时生效并自动保存到当前 profile。</div>
        <div class="set-card">
          <div class="set-row">
            <div class="set-main"><div class="name">外观主题</div>
              <div class="desc">深色 / 浅色 / 跟随系统</div></div>
            <div class="seg" id="themeSeg">
              <button onclick="pickTheme('dark')">🌙 深色</button>
              <button onclick="pickTheme('light')">☀️ 浅色</button>
              <button onclick="pickTheme('system')">💻 跟随系统</button>
            </div>
          </div>
          <div class="set-row">
            <div class="set-main"><div class="name">强调色</div>
              <div class="desc">预设色板或自定义 #RRGGBB</div></div>
          </div>
          <div class="swatches" id="swatches"></div>
          <div class="install-row">
            <input id="accentInput" placeholder="自定义强调色 #RRGGBB，如 #e5588a">
            <button class="ghost" onclick="applyCustomAccent()">应用</button>
          </div>
          <div class="set-row">
            <div class="set-main"><div class="name">界面字体</div>
              <div class="desc">填系统已安装的字体名，留空恢复默认</div></div>
          </div>
          <div class="install-row">
            <input id="fontInput" placeholder="如 微软雅黑 / Consolas">
            <button class="ghost" onclick="applyCustomFont()">应用</button>
          </div>
        </div>
        <div class="modal-footer"><span id="appearanceNote"></span></div>
      </div>
      <div id="tab-general" style="display:none">
        <div class="set-title">通用</div>
        <div class="hint">界面开关与数据管理；更改即时生效。</div>
        <div class="set-card"><div id="generalToggles"></div></div>
        <div class="set-h">文件改动与回滚</div>
        <div class="set-card">
          <div class="set-row">
            <div class="set-main"><div class="name">写文件前显示 diff 确认</div>
              <div class="desc">开启后每次写入都先看一眼差异再决定；关闭时只自动留底（可随时撤销）</div></div>
            <button class="msg-act" id="confirmWriteBtn" onclick="toggleConfirmWrite()">已关闭</button>
          </div>
        </div>
        <div class="set-card"><div id="ckptList"></div></div>
        <div class="set-h">快捷键</div>
        <div class="set-card">
          <div class="set-row"><div class="set-main"><div class="name">新会话</div></div><span class="kbd">Ctrl+N</span></div>
          <div class="set-row"><div class="set-main"><div class="name">搜索会话</div></div><span class="kbd">Ctrl+F</span></div>
          <div class="set-row"><div class="set-main"><div class="name">打开设置</div></div><span class="kbd">Ctrl+,</span></div>
          <div class="set-row"><div class="set-main"><div class="name">显示 / 隐藏左侧栏</div></div><span class="kbd">Ctrl+B</span></div>
          <div class="set-row"><div class="set-main"><div class="name">显示 / 隐藏右侧面板</div></div><span class="kbd">Ctrl+J</span></div>
        </div>
        <div class="set-h">数据</div>
        <div class="set-card">
          <div class="set-row">
            <div class="set-main"><div class="name">备份 profile</div>
              <div class="desc">配置 + 会话 + 记忆 + 定时任务 + 知识库索引 → 工作区 exports/ 下的 zip</div></div>
            <button class="msg-act" onclick="backupProfile()">📦 备份</button>
          </div>
          <div class="set-row">
            <div class="set-main"><div class="name">从备份恢复</div>
              <div class="desc">覆盖同名文件；当前配置先存为 config.pre-import.json</div></div>
            <button class="msg-act" onclick="restoreProfile()">📂 恢复</button>
          </div>
          <div class="set-row">
            <div class="set-main"><div class="name">搜索会话</div>
              <div class="desc">按关键词搜索全部历史并跳转高亮</div></div>
            <button class="msg-act" onclick="settingsSearch()">🔍 搜索</button>
          </div>
          <div class="set-row">
            <div class="set-main"><div class="name">导出当前会话</div>
              <div class="desc">存为 Markdown 到 &lt;工作区&gt;/exports</div></div>
            <button class="msg-act" onclick="settingsExport(false)">📤 导出</button>
          </div>
          <div class="set-row">
            <div class="set-main"><div class="name">导出全部会话</div>
              <div class="desc">每会话一个 Markdown + 目录，打包 zip</div></div>
            <button class="msg-act" onclick="settingsExport(true)">🗂 打包</button>
          </div>
          <div class="set-row">
            <div class="set-main"><div class="name">Git 远程仓库</div>
              <div class="desc">配置 origin（提交/推送的目标仓库）；提交与推送由你手动执行</div></div>
            <button class="msg-act" onclick="gotoReview()">🐙 配置</button>
          </div>
          <div class="set-row">
            <div class="set-main"><div class="name">初始化工程（Bootstrap）</div>
              <div class="desc">目录规范 + .gitignore + git 基线提交；带目标时让 agent 生成 TODO.md</div></div>
            <button class="msg-act" onclick="bootstrapFlow()">🚀 开始</button>
          </div>
        </div>
        <div class="modal-footer"><span id="generalNote"></span></div>
      </div>
      <div id="tab-usage" style="display:none">
        <div class="set-title">用量</div>
        <div class="hint">Token 用量统计（数据来自 profile 的 usage.jsonl，每轮对话一条记录）。</div>
        <div class="stat-cards" id="usageStats"></div>
        <div class="set-h">近 30 天 tokens 折线</div>
        <div id="usageChartHolder"></div>
      </div>
    </div>
  </div>
</div>

<div id="lightbox"><img id="lightboxImg" alt=""></div>
<div id="toast"></div>
<div id="dropMask"><div class="dm-card">松开以添加文件（图片 → 附加发送；文本 → 插入输入框）</div></div>

<script>
const api = () => window.pywebview.api;
const $ = (id) => document.getElementById(id);
let currentSession = 'default';
let currentModel = '';
let perms = { shell: 'ask', fs: 'allow', git: 'ask' };
const PERM_TEXT = { ask: '需确认', allow: '允许', deny: '禁止' };

function esc(s) {
  // 单引号也转义（审计 H-09）：虽然当前所有插值点都是双引号属性，
  // 防未来新增单引号上下文时被模型名等内容注入
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ---------- 轻提示 toast：操作反馈浮层，2.6s 自动消失，不进消息会话 ---------- */
function toast(msg, err) {
  const box = $('toast');
  const item = document.createElement('div');
  item.className = 't-item' + (err ? ' err' : '');
  item.textContent = String(msg == null ? '' : msg);
  box.appendChild(item);
  box.classList.add('show');
  setTimeout(() => {
    item.remove();
    if (!box.children.length) box.classList.remove('show');
  }, 2600);
}
function mdInline(text) {
  // 行内格式（输入已转义、行内代码已占位）：
  // 先 ***x***，再 **x**，最后 *x*（斜体），避免互相错切
  text = text.replace(/\*\*\*([^*\n]+)\*\*\*/g, '<span class="bold">$1</span>');
  text = text.replace(/\*\*([^*\n]+)\*\*/g, '<span class="bold">$1</span>');
  text = text.replace(/(^|[^*\\])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  // 链接：仅 http(s)，防 javascript: 注入
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return text;
}
function isMdTableSep(row) {
  // 分隔行（|---|---| / | :---: | ---: |）：只含 | : - 空格，且至少一段 ---
  const inner = row.replace(/^\|/, '').replace(/\|$/, '');
  return row.includes('-') && /-{3,}/.test(inner) && /^[ :|-]+$/.test(inner);
}
function renderMdTable(rows) {
  // 输入是已转义、行内代码已占位的行；单元格里的 \| 是字面竖线（md 转义），
  // 先换占位符再按 | 切分，切完还原
  const cellsOf = (row) => row.replace(/\\\|/g, '\u0003')
    .replace(/^\|/, '').replace(/\|$/, '')
    .split('|').map(c => c.trim().replace(/\u0003/g, '|'));
  let head = null, aligns = null;
  let bodyRows = rows;
  if (rows.length >= 2 && isMdTableSep(rows[1])) {
    head = cellsOf(rows[0]);
    aligns = cellsOf(rows[1]).map(c =>
      c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : 'left');
    bodyRows = rows.slice(2);
  }
  const n = head ? head.length : 0;
  const alignAttr = (i) =>
    (aligns && aligns[i] && aligns[i] !== 'left') ? ' style="text-align:' + aligns[i] + '"' : '';
  let html = '<table class="md-table">';
  if (head) {
    html += '<thead><tr>' + head.map((c, i) => '<th' + alignAttr(i) + '>' + mdInline(c) + '</th>').join('') + '</tr></thead>';
  }
  html += '<tbody>' + bodyRows.map(r => {
    let cells = cellsOf(r);
    if (n) cells = cells.concat(Array(Math.max(0, n - cells.length)).fill('')).slice(0, n);
    return '<tr>' + cells.map((c, i) => '<td' + alignAttr(i) + '>' + mdInline(c) + '</td>').join('') + '</tr>';
  }).join('') + '</tbody></table>';
  return html;
}
function md(text) {
  let src = String(text == null ? '' : text);
  // 1. 围栏代码块（```lang ... ```）先摘出来，避免其内容被行级规则波及；
  //    原文存进 data-code（esc 后属性安全），工具条按钮从 DOM 读回——
  //    流式期间每帧重渲染，注册表式存储会无限膨胀，挂在元素上则天然自愈
  const fences = [];
  src = src.replace(/```([^\n]*)\n?([\s\S]*?)(?:```|$)/g, (m, lang, code) => {
    const body = code.replace(/\n$/, '');
    fences.push('<div class="fence-wrap" data-code="' + esc(body) + '">' +
      '<div class="fence-bar"><span class="fence-lang">' + esc((lang || '').trim()) + '</span>' +
      '<span class="flex1"></span>' +
      '<button class="fence-btn" onclick="copyFence(this)">复制</button>' +
      '<button class="fence-btn" onclick="applyFence(this)">应用到文件</button>' +
      '</div><pre class="fence">' + esc(body) + '</pre></div>');
    return '\u0001' + (fences.length - 1) + '\u0001';
  });
  // 2. 行内代码占位（P2 #6）
  const codes = [];
  src = src.replace(/`([^`\n]+)`/g, (m, c) => {
    codes.push(c);
    return '\u0000' + (codes.length - 1) + '\u0000';
  });
  // 3. 其余内容整体转义
  src = esc(src);
  // 4. 行级分块：标题 / 列表 / 引用 / 分隔线 / 段落
  const out = [];
  let list = null;             // 'ul' | 'ol' | null
  let quote = false;
  let para = [];
  const closeList = () => { if (list) { out.push('</' + list + '>'); list = null; } };
  const closeQuote = () => { if (quote) { out.push('</blockquote>'); quote = false; } };
  const closePara = () => {
    if (para.length) {
      out.push('<p>' + para.map(mdInline).join('<br>') + '</p>');
      para = [];
    }
  };
  const closeAll = () => { closePara(); closeList(); closeQuote(); };
  const linesArr = src.split('\n');
  for (let li = 0; li < linesArr.length; li++) {
    const line = linesArr[li];   // 已转义；&gt; 即原文的 >
    const fence = line.trim().match(/^\u0001(\d+)\u0001$/);
    if (fence) { closeAll(); out.push(fenceHtml(fences, fence[1])); continue; }
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { closeAll(); out.push('<hr>'); continue; }
    const h = line.match(/^(#{1,4})\s+(.+?)\s*#*$/);
    if (h) { closeAll(); out.push('<div class="md-h md-h' + h[1].length + '">' + mdInline(h[2]) + '</div>'); continue; }
    // 表格（模型回答结构化结果的常用形态）：连续 | 行 + 第二行 |---|---| 分隔行；
    // 不成表格的散落 | 行回落到普通段落
    if (/^\s*\|/.test(line)) {
      const rows = [];
      let j = li;
      while (j < linesArr.length && /^\s*\|/.test(linesArr[j])) { rows.push(linesArr[j].trim()); j++; }
      if (rows.length >= 2 && isMdTableSep(rows[1])) {
        li = j - 1;
        closeAll();
        out.push(renderMdTable(rows));
        continue;
      }
    }
    const ul = line.match(/^\s*[-*•]\s+(.*)$/);
    const ol = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
    if (ul) {
      closePara(); closeQuote();
      if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; }
      // 任务清单：- [ ] / - [x] → ☐ / ☑（展示用，不可交互）
      let item = ul[1];
      const tk = item.match(/^\[( |x|X)\]\s+/);
      let box = '';
      if (tk) {
        item = item.slice(tk[0].length);
        box = tk[1].toLowerCase() === 'x' ? '<span class="task done">☑</span> '
                                          : '<span class="task">☐</span> ';
      }
      out.push('<li>' + box + mdInline(item) + '</li>');
      continue;
    }
    if (ol) {
      closePara(); closeQuote();
      if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; }
      out.push('<li>' + mdInline(ol[2]) + '</li>');
      continue;
    }
    const q = line.match(/^\s*&gt;\s?(.*)$/);
    if (q) {
      closePara(); closeList();
      if (!quote) { out.push('<blockquote>'); quote = true; }
      out.push('<div>' + mdInline(q[1]) + '</div>');
      continue;
    }
    if (!line.trim()) { closeAll(); continue; }
    closeList(); closeQuote();
    para.push(line);
  }
  closeAll();
  let html = out.join('\n');
  // 兜底：不在独立行上的围栏占位符也还原（避免控制字符露出）
  html = html.replace(/\u0001(\d+)\u0001/g, (m, i) => fenceHtml(fences, i));
  // 5. 还原行内代码（内容需再次转义）
  html = html.replace(/\u0000(\d+)\u0000/g, (m, i) => '<code>' + esc(codes[Number(i)]) + '</code>');
  return html;
}
function fenceHtml(fences, idx) {
  return fences[Number(idx)] || '';
}

/* ---------- 代码块工具（复制 / 应用到文件） ---------- */
function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => toast('已复制'),
      () => fallbackCopy(text));
  } else fallbackCopy(text);
}
function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); toast('已复制'); }
  catch (e) { toast('复制失败', true); }
  ta.remove();
}
function copyFence(btn) {
  const wrap = btn.closest('.fence-wrap');
  copyText(wrap ? (wrap.dataset.code || '') : '');
}
async function applyFence(btn) {
  const wrap = btn.closest('.fence-wrap');
  const code = wrap ? (wrap.dataset.code || '') : '';
  const path = await dialogPrompt('应用到文件 —— 保存到工作区路径（相对路径，如 src/app.js）', '');
  if (path === null || !String(path).trim()) return;   // 取消 / 空路径
  const r = await api().apply_code(String(path).trim(), code);
  if (r.ok) {
    toast('已写入 ' + path + '（可 ⬆ 上级旁的 ↶ 撤销）');
    if (curPanel === 'ws') loadWsTree();
  } else {
    toast(r.output || r.error || '写入失败', true);
  }
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
  $('dlgChoose').style.display = 'none';  // 列表对话框与输入/确认互斥，防叠层
  $('dlgRow').style.display = '';
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
  $('dlgChoose').style.display = 'none';
  window.__dialogResult = value;
}
// 取消必须是可区分的 false 而非 null：null 在 Python 侧等于「未响应」，
// 点取消会被当没点而空等超时（H-05）。false = 明确的拒绝。
$('dlgOk').onclick = () => _closeDialog($('dlgInput').style.display !== 'none' ? $('dlgInput').value : true);
$('dlgCancel').onclick = () => _closeDialog(false);
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
        // 取消返回 null（与「确定但留空」的 "" 区分开）——
        // 此前取消返回 false，newSessionFlow 的 `=== null` 判断挡不住，
        // 导致每次点取消都新建出一个 s-... 裸名会话
        resolve(v === false ? null : v);
      }
    }, 100);
  });
}
function dialogConfirm(title, message, confirmText) {
  showDialog({ title, message, confirmText: confirmText || '删除' });
  return new Promise(resolve => {
    const timer = setInterval(() => {
      if (window.__dialogResult !== null) {
        clearInterval(timer);
        const v = window.__dialogResult;
        window.__dialogResult = null;
        resolve(v === true);
      }
    }, 100);
  });
}
function dialogChoose(title, options) {
  // 列表选择对话框：options = [{value, label, sub?}]；取消返回 null
  const list = $('dlgChoose');
  list.innerHTML = '';
  for (const opt of options) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'choose-item';
    btn.innerHTML = '<span class="choose-label">' + esc(opt.label) + '</span>' +
      (opt.sub ? '<span class="choose-sub">' + esc(opt.sub) + '</span>' : '');
    btn.onclick = () => _closeDialog(opt.value);
    list.appendChild(btn);
  }
  $('dlgTitle').textContent = title || '';
  $('dlgMsg').style.display = 'none';
  $('dlgInput').style.display = 'none';
  $('dlgRow').style.display = 'none';  // 列表用条目直接确认，不要「确定」按钮
  list.style.display = 'block';
  $('dlgOverlay').style.display = 'flex';
  return new Promise(resolve => {
    const timer = setInterval(() => {
      if (window.__dialogResult !== null) {
        clearInterval(timer);
        const v = window.__dialogResult;
        window.__dialogResult = null;
        resolve(v === false || v === true ? null : v);
      }
    }, 100);
  });
}

/* ---------- 多模型并答对比（⚖）：同一问题并行问两个模型，不进会话历史 ---------- */
function addCompareCard() {
  const div = document.createElement('div');
  div.className = 'msg bot';
  div.innerHTML = '<div class="cmp-grid">' +
    '<div class="cmp-pane"><div class="cmp-head">…</div><div class="cmp-body hint">生成中…</div></div>' +
    '<div class="cmp-pane"><div class="cmp-head">…</div><div class="cmp-body hint">生成中…</div></div>' +
    '</div>';
  $('thread').appendChild(div);
  $('log').scrollTop = $('log').scrollHeight;
  return div;
}
function fillCompareCard(div, res) {
  const sides = [res.a, res.b];
  div.querySelectorAll('.cmp-pane').forEach((pane, i) => {
    const s = sides[i] || { ok: false, error: '无结果' };
    pane.querySelector('.cmp-head').textContent =
      (s.name || '?') + (s.elapsed_ms ? ' · ' + (s.elapsed_ms / 1000).toFixed(1) + 's' : '');
    const body = pane.querySelector('.cmp-body');
    body.classList.remove('hint');
    body.innerHTML = s.ok
      ? md(s.reply || '（空回复）')
      : '<span class="cmp-err">' + esc(s.error || '失败') + '</span>';
  });
  $('log').scrollTop = $('log').scrollHeight;
}
async function compareFlow() {
  const input = $('input');
  const text = input.value.trim();
  if (!text) { toast('先输入要对比的问题', true); return; }
  const s = await api().get_models();
  const names = (s.models || []).map(m => m.name).filter(n => n && n !== currentModel);
  if (!names.length) { toast('池子里只有一个模型——先在设置里加第二个模型才能对比', true); return; }
  const pick = await dialogChoose('与哪个模型对比？（当前：' + (currentModel || '?') + '）',
    names.map(n => ({ value: n, label: n, sub: '' })));
  if (!pick) return;
  input.value = '';
  input.style.height = 'auto';
  add(text, 'user');
  atClose();
  const card = addCompareCard();
  setBusy(true);
  const res = await api().compare_models(text, currentModel || '', pick);
  setBusy(false);
  fillCompareCard(card, res);
  refreshStatus();
}

/* ---------- 会话分支（⎇）：从某条回复处分叉出新会话，原会话不动 ---------- */
async function branchFrom(sid, hi) {
  const r = await api().branch_session(sid, hi);
  if (r && r.ok) {
    toast('已分叉新会话（含该条及之前的 ' + r.messages + ' 条），在侧栏查看');
    refreshSidebar();
  } else {
    toast((r && r.error) || '分支失败', true);
  }
}

/* ---------- 主题 ---------- */
let theme = 'system';
const THEME_TEXT = { dark: '深色', light: '浅色', system: '跟随系统' };
function applyTheme(mode) {
  theme = mode;
  document.documentElement.dataset.theme = mode;
}

/* ---------- 消息渲染 ---------- */

function add(text, cls, meta, reasoning, hi, tools) {
  $('hero').style.display = 'none';
  text = (text == null ? '' : String(text));
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  if (typeof hi === 'number') div.dataset.hi = String(hi);  // 搜索跳转定位用
  if (cls === 'user') {
    div.textContent = text || '（空）';
  } else {
    if (reasoning) {
      // 「思考关」只是请求不思考；服务商忽略开关返回的内容照常折叠展示
      const rd = document.createElement('div');
      rd.className = 'reasoning';
      const rounds = lastRounds > 1 ? '（' + lastRounds + ' 轮）' : '';
      rd.innerHTML = '<div class="r-head"><span class="r-title">深度思考' + rounds +
        '</span><span class="r-caret-end">›</span></div>' +
        '<div class="r-body"></div>';
      rd.querySelector('.r-body').textContent = reasoning;
      rd.querySelector('.r-head').onclick = () => rd.classList.toggle('open');
      div.appendChild(rd);
    }
    // 工具调用卡片：按「更改 / 运行命令 / 其他」分组，可折叠（写文件等操作直接可见）
    if (tools && tools.length) {
      div.appendChild(renderToolGroups(tools));
    }
    const body = document.createElement('div');
    body.className = 'md-body';
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
  // 消息操作条：bot 消息可重新生成（仅最后一条可见）；回放消息可 ⎇ 分支
  // （原「复制」「✎ 编辑」按钮已按用户要求先后移除——选中文本即可复制）
  if (cls !== 'user') {
    const acts = document.createElement('div');
    acts.className = 'msg-acts';
    const rg = document.createElement('button');
    rg.className = 'msg-act msg-regen'; rg.textContent = '↻ 重新生成';
    rg.onclick = regenerateLast;
    acts.appendChild(rg);
    // 分支仅在回放消息上出现（有 hi 序号）：把该条及之前的历史复制成新会话
    if (typeof hi === 'number') {
      const br = document.createElement('button');
      br.className = 'msg-act';
      br.textContent = '⎇ 分支';
      br.onclick = () => branchFrom(currentSession, hi);
      acts.appendChild(br);
    }
    div.appendChild(acts);
  }
  $('thread').appendChild(div);
  $('log').scrollTop = $('log').scrollHeight;
  updateRegenVisibility();
  return div;
}

function updateRegenVisibility() {
  // 「重新生成」只对最后一条 bot 消息可见（它的语义是重跑最后一轮）
  const bots = document.querySelectorAll('#thread .msg.bot');
  const last = bots.length ? bots[bots.length - 1] : null;
  document.querySelectorAll('#thread .msg-regen').forEach(b => {
    b.style.display = (b.closest('.msg') === last) ? '' : 'none';
  });
}

/* ---------- 重新生成 ---------- */
async function regenerateLast() {
  const ok = await dialogConfirm('重新生成',
    '将丢弃最后一条回复及其后的内容并重新生成，继续？');
  if (!ok) return;
  showLiveBlock(); setBusy(true);
  const r = await api().regenerate_last();
  removeLiveBlock(); setBusy(false);
  if (!r.ok) { add(r.error || '未知错误', 'bot error'); return; }
  await selectSession(currentSession);
  notifyDone();
}

function showHeroIfEmpty() {
  const empty = !$('thread').children.length;
  $('hero').style.display = empty ? 'flex' : 'none';
  // 新会话时才显示工作区选择（对齐 dsh）；用户手动隐藏后不再自动弹出
  $('wsBar').style.display = (empty && !wsBarDismissed) ? 'flex' : 'none';
}

/* ---------- 工作区选择条：隐藏 / 找回 ---------- */
// pywebview 的 html= 模式下 localStorage 可能不可用，故用安全包装 + 后端 config 双通道
let wsBarDismissed = false;

function lsGet(key) {
  try { return window.localStorage ? localStorage.getItem(key) : null; }
  catch (err) { return null; }
}
function lsSet(key, val) {
  try { if (window.localStorage) localStorage.setItem(key, val); } catch (err) { /* 忽略 */ }
}
let uiPrefs = {};   // 提示音 / 通知 / 强调色 / 字体（与面板宽度等一起持久化）

function saveUiPrefs() {
  const panel = $('rightPanel');
  const side = document.querySelector('aside');
  const prefs = {
    wsbar_hidden: wsBarDismissed,
    rp_width: panel ? Math.round(panel.getBoundingClientRect().width) : 0,
    side_width: side && !sideHidden ? Math.round(side.getBoundingClientRect().width) : 0,
    side_hidden: sideHidden,
    rp_hidden: rpHidden,
    rp_tab_order: rpTabOrder.slice(),
    notify_sound: !!uiPrefs.notify_sound,
    notify_desktop: !!uiPrefs.notify_desktop,
    show_done: !!uiPrefs.show_done,
    accent: uiPrefs.accent || '',
    font: uiPrefs.font || '',
  };
  lsSet('sh_wsbar_hidden', wsBarDismissed ? '1' : '0');
  lsSet('sh_rp_width', String(prefs.rp_width));
  lsSet('sh_side_width', String(prefs.side_width));
  lsSet('sh_accent', prefs.accent);
  lsSet('sh_font', prefs.font);
  if (window.pywebview && window.pywebview.api) {
    window.pywebview.api.set_ui_prefs(prefs).catch(() => {});
  }
}
function applyUiPrefs(prefs) {
  if (!prefs) return;
  if (typeof prefs.wsbar_hidden === 'boolean') wsBarDismissed = prefs.wsbar_hidden;
  const apply = (el, w, lo, hi) => {
    if (el && w >= lo && w <= hi) { el.style.width = w + 'px'; el.style.minWidth = w + 'px'; }
  };
  apply($('rightPanel'), parseInt(prefs.rp_width || '0', 10), 260, 900);
  apply(document.querySelector('aside'), parseInt(prefs.side_width || '0', 10), 180, 460);
  if (Array.isArray(prefs.rp_hidden)) rpHidden = prefs.rp_hidden.filter(n => typeof n === 'string');
  if (Array.isArray(prefs.rp_tab_order) && prefs.rp_tab_order.length) {
    // 标签页顺序持久化：只接受合法项，缺漏的按默认序补齐
    const valid = prefs.rp_tab_order.filter(n => TAB_LABELS[n]);
    for (const n of RP_TAB_ORDER_DEFAULT) {
      if (!valid.includes(n)) valid.push(n);
    }
    rpTabOrder = valid;
    renderTabs();
  }
  if (typeof prefs.side_hidden === 'boolean') toggleSide(prefs.side_hidden);
  // 外观个性化：强调色 / 字体 / 完成提示音 / 窗口通知
  uiPrefs = {
    notify_sound: !!prefs.notify_sound,
    notify_desktop: !!prefs.notify_desktop,
    show_done: !!prefs.show_done,
    accent: typeof prefs.accent === 'string' ? prefs.accent : '',
    font: typeof prefs.font === 'string' ? prefs.font : '',
  };
  applyAccent(uiPrefs.accent);
  applyFont(uiPrefs.font);
  applyTabVisibility();
  showHeroIfEmpty();
}

/* ---------- 左侧栏显隐 ---------- */
let sideHidden = false;

function toggleSide(force) {
  sideHidden = typeof force === 'boolean' ? force : !sideHidden;
  document.body.classList.toggle('side-hidden', sideHidden);
  const b = $('sideBtn');
  if (b) b.classList.toggle('off', sideHidden);
  saveUiPrefs();
}

function dismissWsBar() {
  wsBarDismissed = true;
  $('wsBar').style.display = 'none';
  saveUiPrefs();
}
function toggleWsBar() {
  if (wsBarDismissed) {
    wsBarDismissed = false;
    showHeroIfEmpty();  // 空会话时重新显示
    saveUiPrefs();
  } else {
    dismissWsBar();
  }
}

/* ---------- 右侧面板宽度拖拽 ---------- */
(function initPanelResize() {
  const grip = $('rpResize');
  const panel = $('rightPanel');
  if (!grip || !panel) return;
  let dragging = false, startX = 0, startW = 0;
  grip.addEventListener('mousedown', (e) => {
    dragging = true; startX = e.clientX;
    startW = panel.getBoundingClientRect().width;
    grip.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const max = Math.min(760, window.innerWidth - 380);
    const w = Math.max(260, Math.min(max, startW + (startX - e.clientX)));
    panel.style.width = w + 'px';
    panel.style.minWidth = w + 'px';
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    grip.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    saveUiPrefs();
  });
  // 先用 localStorage 快速恢复（避免等后端时闪一下）
  const saved = parseInt(lsGet('sh_rp_width') || '0', 10);
  if (saved >= 260 && saved <= 900) {
    panel.style.width = saved + 'px';
    panel.style.minWidth = saved + 'px';
  }
  if (lsGet('sh_wsbar_hidden') === '1') wsBarDismissed = true;
})();

/* ---------- 状态刷新 ---------- */
async function refreshStatus() {
  const s = await api().status();
  currentModel = s.model || '';
  perms = { shell: s.shell_permission || 'ask', fs: s.fs_permission || 'allow',
            git: s.git_permission || 'ask' };
  confirmWriteOn = !!s.confirm_write;
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
  $('planBtn').classList.toggle('on', !!s.plan_mode);
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
  // 完成提醒开关（提示音 / 窗口通知）常驻卡片底部
  if ((s.percent || 0) >= 80) {
    const warn = document.createElement('div');
    warn.className = 'note-err';
    warn.style.marginTop = '10px';
    warn.textContent = '上下文水位已超过 80%，长任务建议无状态重置。';
    card.appendChild(warn);
  }
  const notifyTitle = document.createElement('div');
  notifyTitle.className = 'side-label';
  notifyTitle.style.margin = '12px 0 2px';
  notifyTitle.textContent = '完成提醒';
  card.appendChild(notifyTitle);
  const mkNotify = (label, key) => {
    const row = document.createElement('div');
    row.className = 'ctx-row';
    const dot = document.createElement('span');
    dot.className = 'dot' + (uiPrefs[key] ? '' : ' dim');
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = label;
    const btn = document.createElement('button');
    btn.className = 'msg-act';
    btn.textContent = uiPrefs[key] ? '已开启' : '已关闭';
    btn.onclick = async () => {
      await toggleNotify(key);
      btn.textContent = uiPrefs[key] ? '已开启' : '已关闭';
      dot.classList.toggle('dim', !uiPrefs[key]);
    };
    row.append(dot, name, btn);
    return row;
  };
  card.appendChild(mkNotify('回复完成提示音', 'notify_sound'));
  card.appendChild(mkNotify('窗口通知（最小化时）', 'notify_desktop'));
  // 一键无状态重置（F6）：总结写回 TODO.md → 自动新会话
  const resetRow = document.createElement('div');
  resetRow.className = 'ctx-row';
  const rname = document.createElement('span');
  rname.className = 'name';
  rname.textContent = '无状态重置（进展 → TODO.md → 新会话）';
  const rbtn = document.createElement('button');
  rbtn.className = 'msg-act';
  rbtn.textContent = '⟳ 重置';
  rbtn.onclick = () => { card.classList.remove('open'); statelessReset(); };
  resetRow.append(rname, rbtn);
  card.appendChild(resetRow);
  card.classList.add('open');
  $('permMenu').classList.remove('open');
}

/* ---------- Token 用量（含缓存命中率） ---------- */
function cachedOf(u) {
  // 不同服务商的缓存命中字段位置不同，防御式提取
  if (!u) return 0;
  if (u.cached_tokens) return Number(u.cached_tokens) || 0;
  if (u.prompt_tokens_details && u.prompt_tokens_details.cached_tokens)
    return Number(u.prompt_tokens_details.cached_tokens) || 0;
  return 0;
}

async function refreshUsage() {
  const res = await api().usage();
  const u = res.usage || {};
  const p = Number(u.prompt_tokens) || 0;
  const c = Number(u.completion_tokens) || 0;
  if (!p && !c) { $('usageLine').textContent = ''; return; }
  let line = '输入 ' + fmtTokens(p) + ' · 输出 ' + fmtTokens(c) + ' tokens';
  const cached = cachedOf(u);
  if (cached && p) {
    line += ' · 缓存命中 ' + Math.round(cached * 100 / p) + '%';
  } else if (res.source === 'llm') {
    // 真实用量帧（非估算）但服务商没报缓存命中：明确显示 0%，而不是藏起来
    line += ' · 缓存命中 0%';
  }
  if (lastSpeed) line += ' · ' + lastSpeed + ' tok/s';
  $('usageLine').textContent = line;
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
  // 侧栏会话过滤框：按名称/id 即时过滤，无匹配的工作区分组整组隐藏
  const q = (($('sessionFilter') || {}).value || '').trim().toLowerCase();
  let visible = q
    ? sidebar.sessions.filter(s => (s.name + ' ' + s.id).toLowerCase().includes(q))
    : sidebar.sessions;
  if (!uiPrefs.show_done) {
    // 已完成的会话默认隐藏（当前会话除外，避免切走后界面空掉）
    visible = visible.filter(s => !s.done || s.id === currentSession);
  }
  let anyShown = false;
  for (const ws of sidebar.workspaces) {
    const sessList = visible.filter(x => x.ws === ws.id);
    if (q && !sessList.length) continue;
    anyShown = true;
    const group = document.createElement('div');
    group.className = 'ws-group' + (collapsedWs.has(ws.id) ? ' collapsed' : '');
    const head = document.createElement('div');
    head.className = 'ws-head' + (ws.id === currentWs ? ' cur' : '');
    head.title = '点击切换到此工作区（⌄ 折叠会话，⋯ 重命名）';
    head.innerHTML = '<button class="ws-caret" title="折叠/展开会话">▾</button><span>📁</span>' +
      '<span class="ws-name">' + esc(ws.name) + '</span>' +
      '<button class="row-menu" title="重命名工作区">⋯</button>';
    // 点击工作区名 = 切换（同步 composer 显示与右侧文件树）
    head.onclick = (e) => { if (!e.target.closest('.row-menu')) switchWorkspace(ws.path); };
    head.querySelector('.ws-caret').onclick = (e) => {
      e.stopPropagation();
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
    for (const sess of sessList) {
      const row = document.createElement('div');
      row.className = 's-row' + (sess.id === currentSession ? ' active' : '') +
        (sess.done ? ' done' : '');
      row.innerHTML = '<span class="s-name">' + esc(sess.name) + '</span>' +
        '<span class="s-time">' + relTime(sess.updated) + '</span>' +
        '<button class="pin' + (sess.pinned ? ' on' : '') + '" title="置顶/取消置顶（置顶后排在本工作区最前）">★</button>' +
        '<button class="row-done' + (sess.done ? ' on' : '') + '" title="标记完成/未完成（完成的排最后并划线）">✓</button>' +
        '<button class="row-menu" title="重命名会话">⋯</button>' +
        '<button class="row-menu row-del" title="删除会话">🗑</button>';
      // 拖拽排序：仅同工作区分组内可拖（跨组意味着换工程，不开放）。
      // 用鼠标事件自实现（而非 HTML5 原生拖拽）——原生拖拽在 WebView2 里
      // 会渲染系统级拖影层（中间的大白框），鼠标方案完全无拖影，
      // 位置反馈由目标行上的虚线框承担
      row.__sess = sess;
      row.title = '按住拖动可排序；点击打开会话';
      row.onmousedown = (e) => {
        if (e.button !== 0 || e.target.closest('button')) return;  // 按钮点击不进入拖拽
        const startX = e.clientX;
        const startY = e.clientY;
        let active = false;
        const move = (ev) => {
          if (!active) {
            // 位移超阈值才算拖拽（横竖任一方向）
            if (Math.abs(ev.clientX - startX) < 5 &&
                Math.abs(ev.clientY - startY) < 5) return;
            active = true;
            row.classList.add('dragging');
            document.body.classList.add('sess-dragging');
          }
          ev.preventDefault();  // 拖拽中禁止选择文本
          const t = document.elementFromPoint(ev.clientX, ev.clientY);
          const target = t && t.closest ? t.closest('.s-row') : null;
          const ok = target && target.__sess && target !== row &&
                     target.__sess.ws === sess.ws && target.__sess.id !== sess.id;
          document.querySelectorAll('.s-row.dragover').forEach(x => {
            if (x !== (ok ? target : null)) x.classList.remove('dragover');
          });
          if (ok) target.classList.add('dragover');
        };
        const up = (ev) => {
          document.removeEventListener('mousemove', move);
          document.removeEventListener('mouseup', up);
          document.body.classList.remove('sess-dragging');
          row.classList.remove('dragging');
          const target = document.querySelector('.s-row.dragover');
          document.querySelectorAll('.s-row.dragover').forEach(x => x.classList.remove('dragover'));
          if (!active) return;  // 位移不足 = 普通点击，交给 onclick
          if (!target) return;
          const ts = target.__sess;
          if (!ts || ts.ws !== sess.ws || ts.id === sess.id) return;
          const seq = sessList.map(x => x.id);
          const from = seq.indexOf(sess.id);
          const to = seq.indexOf(ts.id);
          if (from < 0 || to < 0) return;
          seq.splice(from, 1);
          seq.splice(to, 0, sess.id);
          api().set_session_order(seq).then(refreshSidebar);
        };
        document.addEventListener('mousemove', move);
        document.addEventListener('mouseup', up);
      };
      row.onclick = () => selectSession(sess.id);
      row.querySelector('.pin').onclick = async (e) => {
        e.stopPropagation();
        await api().set_session_pin(sess.id, !sess.pinned);
        refreshSidebar();
      };
      row.querySelector('.row-done').onclick = async (e) => {
        e.stopPropagation();
        await api().mark_session_done(sess.id, !sess.done);
        refreshSidebar();
      };
      row.querySelector('.row-menu:not(.row-del)').onclick = async (e) => {
        e.stopPropagation();
        const name = await dialogPrompt('重命名会话', sess.name);
        if (name !== null && name.trim()) {
          await api().rename_session(sess.id, name.trim());
          refreshSidebar();
        }
      };
      row.querySelector('.row-del').onclick = async (e) => {
        e.stopPropagation();
        await deleteSession(sess.id, sess.name);
      };
      group.appendChild(row);
    }
    if (!q && !group.querySelector('.s-row')) {
      const empty = document.createElement('div');
      empty.className = 's-row';
      empty.style.opacity = '.45';
      empty.innerHTML = '<span class="s-name">（暂无会话）</span>';
      group.appendChild(empty);
    }
    tree.appendChild(group);
  }
  if (q && !anyShown) {
    tree.innerHTML = '<div class="hint" style="padding:6px 10px">没有匹配「' +
      esc(q) + '」的会话。</div>';
  }
  if (!sidebar.workspaces.length) {
    tree.innerHTML = '<div class="hint" style="padding:6px 10px">选择工作区后，会话会按项目分组显示。</div>';
  }
}

async function selectSession(id) {
  const s = await api().session_history(id);
  currentSession = id;
  $('thread').innerHTML = '';
  // 思考过程随 assistant 消息落盘（chat_loop._attach_reasoning），回放时重建折叠块
  (s.history || []).filter(m => (m.content || '').trim()).forEach((m, i) =>
    add(m.content, m.role === 'user' ? 'user' : 'bot', null, m.reasoning || '', i));
  showHeroIfEmpty();
  updateSendState();  // 发送按钮按新会话的忙状态恢复
  refreshSidebar();
}

async function deleteSession(id, name) {
  const ok = await dialogConfirm('删除会话',
    '确定删除会话「' + (name || id) + '」？历史记录将一并删除，此操作不可恢复。');
  if (!ok) return;
  const res = await api().delete_session(id);
  if (!res.ok) {
    toast('删除失败: ' + (res.error || '未知错误'), true);
    return;
  }
  if (id === currentSession) {
    await selectSession(res.next || id);  // 后端已切到最近会话（或新建了一个）
  } else {
    refreshSidebar();
  }
}

async function newSessionFlow() {
  // 第一步：选择会话归属的工作区（取消 → 不创建任何会话）
  const wsRes = await api().workspaces();
  const list = (wsRes.workspaces || []);
  const pathOf = {};
  const options = list.map(w => {
    pathOf[w.id] = w.path;
    return {
      value: w.id,
      label: (w.id === wsRes.current ? '✓ ' : '📁 ') + w.name,
      sub: w.path,
    };
  });
  options.push({ value: '__browse__', label: '📂 选择其他文件夹…', sub: '' });
  const wsPick = await dialogChoose('新会话放到哪个工作区？', options);
  if (wsPick === null) return;
  // 第二步：命名（取消或留空 → 不创建，避免出现 s-... 裸名会话）
  const name = await dialogPrompt('新会话名称', '');
  if (name === null || !name.trim()) return;
  // 选定的工作区与当前不同 → 先切换（会话按创建时的工作区归组）
  if (wsPick === '__browse__') {
    const picked = await api().choose_workspace();
    if (!picked.ok) return;  // 用户取消选文件夹 → 放弃创建
  } else if (wsPick !== wsRes.current) {
    await switchWorkspace(pathOf[wsPick]);
  }
  const s = await api().new_session();
  if (name.trim()) await api().rename_session(s.session, name.trim());
  currentSession = s.session;
  $('thread').innerHTML = '';
  wsPath = '';
  showHeroIfEmpty();
  refreshStatus();
  refreshSidebar();
  loadWsTree();  // 右侧文件树同步到新工作区
}

/* ---------- 实时步骤（模型运作过程，对齐 dsh） ---------- */
let liveBlock = null;
let liveLlmCount = 0;
let lastRounds = 0;   // 最近一轮对话的 LLM 轮数（展示在「已深度思考」标题里）

function nearBottom(el, margin) {
  // 用户是否停在底部附近：流式输出只在吸底时自动滚动，不打断向上翻阅
  return el.scrollHeight - el.scrollTop - el.clientHeight < (margin || 90);
}

/* ---------- 权限审批（allow_once / allow_always / deny） ---------- */
let approvalQueue = [];   // 同时到来的多个确认请求排队展示
let approvalCur = null;

function showApproval(evt) {
  approvalQueue.push(evt);
  if (!approvalCur) renderApproval();
}

function renderApproval() {
  const mask = $('approveMask');
  if (!approvalCur && approvalQueue.length) {
    approvalCur = approvalQueue.shift();
  }
  if (!approvalCur) { mask.classList.remove('open'); return; }
  const evt = approvalCur;
  const isWrite = (evt.approval_kind || evt.kind) === 'write';
  $('apTitle').textContent = isWrite ? '写入文件确认' : '执行命令确认';
  $('apCount').textContent = approvalQueue.length ? ('另有 ' + approvalQueue.length + ' 个待确认') : '';
  const box = $('apDetail');
  box.innerHTML = '';
  if (isWrite) {
    const p = document.createElement('div');
    p.className = 'ap-note';
    p.textContent = '📄 ' + (evt.path || '') + ' · ' + (evt.detail || '');
    box.appendChild(p);
    const pre = document.createElement('div');
    pre.className = 'ap-diff';
    pre.innerHTML = renderDiff(evt.diff || '');
    box.appendChild(pre);
  } else {
    const pre = document.createElement('div');
    pre.className = 'ap-cmd';
    pre.textContent = evt.command || '';
    box.appendChild(pre);
    const p = document.createElement('div');
    p.className = 'ap-note';
    p.textContent = '工作目录：' + (evt.cwd ? evt.cwd : '（工作区根）') +
      '　·　「一直允许」会把该命令加入白名单';
    box.appendChild(p);
  }
  $('apAlways').style.display = isWrite ? '' : '';
  mask.classList.add('open');
}

function renderDiff(text) {
  return text.split('\n').map(line => {
    let color = '';
    if (line.startsWith('+') && !line.startsWith('+++')) color = 'd-add';
    else if (line.startsWith('-') && !line.startsWith('---')) color = 'd-del';
    else if (line.startsWith('@@') || line.startsWith('+++') || line.startsWith('---')) color = 'd-meta';
    return color ? '<span class="' + color + '">' + esc(line) + '</span>' : esc(line);
  }).join('\n');
}

async function approveReply(decision) {
  if (!approvalCur) return;
  const id = approvalCur.id;
  approvalCur = null;
  $('approveMask').classList.remove('open');
  await api().resolve_approval(id, decision);
  if (approvalQueue.length) renderApproval();
}

/* ---------- 文件改动检查点：撤销 ---------- */
async function undoLastChange() {
  const r = await api().checkpoints(1);
  const item = (r.items || [])[0];
  if (!item) { toast('没有可撤销的改动记录'); return; }
  const names = (item.files || []).map(f => f.rel).slice(0, 6).join('\n');
  const ok = await dialogConfirm('撤销上一次文件改动',
    '将回滚以下文件到改动前状态（原样恢复，新建的文件会被删除）：\n\n' + names +
    (item.files.length > 6 ? '\n…共 ' + item.files.length + ' 个文件' : ''));
  if (!ok) return;
  const res = await api().undo_checkpoint(item.id);
  if (!res.ok) { toast(res.error || '撤销失败', true); return; }
  toast('已撤销：恢复 ' + (res.restored || 0) + ' 个文件，删除 ' + (res.deleted || 0) + ' 个新建文件');
  loadWsTree();
  refreshSidebar();
  if (curPanel === 'sub') loadSubagents();
}

async function loadCheckpoints() {
  const box = $('ckptList');
  if (!box) return;
  box.innerHTML = '<div class="hint">加载中…</div>';
  const r = await api().checkpoints(8);
  const items = r.items || [];
  box.innerHTML = '';
  if (!items.length) {
    box.innerHTML = '<div class="hint">还没有改动记录。每次写文件前会自动留底，' +
      '改错了一键回滚。</div>';
    return;
  }
  for (const it of items) {
    const row = document.createElement('div');
    row.className = 'set-row';
    const main = document.createElement('div');
    main.className = 'set-main';
    const when = it.ts ? new Date(it.ts * 1000).toLocaleString('zh-CN') : '';
    main.innerHTML = '<div class="name">' + esc(it.label || '文件改动') + '</div>' +
      '<div class="desc">' + esc(when) + ' · ' +
      esc((it.files || []).map(f => f.rel).slice(0, 3).join('、')) +
      ((it.files || []).length > 3 ? ' 等 ' + it.files.length + ' 个文件' : '') + '</div>';
    const btn = document.createElement('button');
    btn.className = 'msg-act';
    btn.textContent = '🔍 diff';
    btn.onclick = async () => {
      const d = await api().checkpoint_diff(it.id);
      showDialog({ title: '改动对照 · ' + (it.label || it.id),
                   message: (d.ok ? d.diff : (d.error || '读取失败')) || '（空）',
                   confirmText: '关闭', cancelText: '关闭' });
    };
    const undo = document.createElement('button');
    undo.className = 'msg-act';
    undo.textContent = '↶ 撤销';
    undo.onclick = async () => {
      const res = await api().undo_checkpoint(it.id);
      if (!res.ok) { toast(res.error || '撤销失败', true); return; }
      toast('已撤销：恢复 ' + (res.restored || 0) + ' 个文件，删除 ' + (res.deleted || 0) + ' 个');
      loadCheckpoints();
      loadWsTree();
    };
    row.append(main, btn, undo);
    box.appendChild(row);
  }
}

/* ---------- 工具调用的展示：图标 / 文案 / 分组卡片 ---------- */
function toolIcon(name) {
  const n = String(name || '');
  if (n === 'write_file' || n === 'edit_file') return '✏️';
  if (n === 'run_command') return '⌨';
  if (n === 'read_file' || n === 'list_files' || n === 'search_code') return '🔍';
  return '🔧';
}

function toolGroupKey(name) {
  const n = String(name || '');
  if (n === 'write_file' || n === 'edit_file') return 'edit';
  if (n === 'run_command') return 'cmd';
  return 'other';
}

function fileFromToolDetail(detail) {
  // 「已写入 a/b.py（…）」/「已修改 a/b.py（…）」→ a/b.py
  const m = String(detail || '').match(/^(?:已写入|已修改|已删除|已移动)\s+(\S+?)[（(]/);
  return m ? m[1] : '';
}

function splitPath(path) {
  const p = String(path || '');
  const idx = p.lastIndexOf('/');
  if (idx < 0) return { name: p, dir: '' };
  return { name: p.slice(idx + 1), dir: p.slice(0, idx + 1) };
}

function toolLabel(name, args, detail) {
  const n = String(name || '');
  const path = (args && args.path) || fileFromToolDetail(detail);
  if (n === 'run_command') return '运行命令 · ' + String((args && args.command) || '').slice(0, 60);
  if (n === 'write_file' || n === 'edit_file') return (n === 'write_file' ? '写入 ' : '编辑 ') + String(path || '');
  if (n === 'read_file') return '读取 ' + String(path || '');
  if (n === 'list_files') return '列出文件 · ' + String((args && args.pattern) || '').slice(0, 40);
  if (n === 'search_code') return '搜索 · ' + String((args && args.pattern) || '').slice(0, 40);
  return n + (path ? ' · ' + path : '');
}

function renderToolGroups(tools) {
  const wrap = document.createElement('div');
  wrap.className = 'tgroups';
  const defs = [
    { key: 'edit', title: '✏️ 更改', unit: '个文件' },
    { key: 'cmd', title: '⌨ 运行命令', unit: '条' },
    { key: 'other', title: '🔧 工具调用', unit: '次' },
  ];
  for (const def of defs) {
    const items = tools.filter(t => toolGroupKey(t.name) === def.key);
    if (!items.length) continue;
    wrap.appendChild(renderToolGroup(def, items));
  }
  return wrap;
}

function renderToolGroup(def, items) {
  const box = document.createElement('div');
  box.className = 'tgroup' + (def.key === 'cmd' ? ' tgroup-cmd' : '');
  const failed = items.filter(t => !t.ok).length;
  const head = document.createElement('div');
  head.className = 'tgroup-head';
  head.innerHTML = '<span class="tg-caret">▸</span><span class="tg-title">' + def.title +
    ' · ' + items.length + ' ' + def.unit + '</span>' +
    (failed ? '<span class="tg-bad">' + failed + ' 个执行失败</span>' : '') +
    '<span class="sp"></span>';
  head.onclick = () => box.classList.toggle('open');
  const body = document.createElement('div');
  body.className = 'tgroup-body';
  for (const t of items) body.appendChild(renderToolRow(def.key, t));
  box.append(head, body);
  return box;
}

function renderToolRow(kind, t) {
  const row = document.createElement('div');
  row.className = 'trow' + (t.ok ? '' : ' bad');
  row.title = t.detail || '';
  if (kind === 'edit') {
    const { name, dir } = splitPath(t.file);
    const verb = t.name === 'write_file' ? '写入' : '编辑';
    const plus = t.plus === null ? '' :
      '<span class="tplus">+' + t.plus + '</span><span class="tminus">-' + t.minus + '</span>';
    row.innerHTML = '<span class="tverb">' + verb + '</span>' +
      '<span class="tfile">' + esc(name || t.file || '?') + '</span>' +
      (dir ? '<span class="tdir">' + esc(dir) + '</span>' : '') +
      plus + (!t.ok ? '<span class="tbadge">执行失败</span>' : '');
  } else if (kind === 'cmd') {
    const cmd = String((t.args && t.args.command) || '');
    row.innerHTML = '<span class="tverb">运行</span>' +
      '<span class="tcmd">' + esc(cmd || '(未知命令)') + '</span>' +
      (!t.ok ? '<span class="tbadge">执行失败</span>' : '');
  } else {
    row.innerHTML = '<span class="tverb">' + esc(t.name) + '</span>' +
      '<span class="tdir">' + esc((t.detail || '').slice(0, 60)) + '</span>';
  }
  return row;
}

function onAgentEvent(evt) {
  // 权限确认最先处理：它可能属于后台会话（并行/子 agent），不能被会话过滤挡掉
  if (evt.kind === 'approval') { showApproval(evt); return; }
  // 多会话并行：非当前会话的思考/工具步骤不渲染到当前线程
  if (evt.session && evt.session !== currentSession) return;
  if (!liveBlock) return;
  const steps = liveBlock.querySelector('.steps');
  if (evt.kind === 'llm') {
    liveLlmCount++;
    // 思考条目：带 reasoning 摘要，悬停可看全文
    const div = document.createElement('div');
    div.className = 'step pending';
    const reasoning = (evt.reasoning || '').trim();
    div.innerHTML = '<span class="s-icon">💭</span><span class="s-text">深度思考 · 第 ' +
      liveLlmCount + ' 轮' + (evt.tools && evt.tools.length ? ' · 准备调用 ' + evt.tools.join(', ') : '') + '</span>';
    if (reasoning) div.title = reasoning;
    steps.appendChild(div);
  } else if (evt.kind === 'tool') {
    const detail = (evt.result || '').split('\n')[0].slice(0, 120);
    const args = evt.args || {};
    const ok = !/^错误/.test(detail);
    const stat = detail.match(/\+(\d+)\s+-(\d+)/) || [];
    // 收集到本轮工具列表：收尾时按「更改 / 运行命令 / 其他」分组渲染成卡片
    liveTools.push({
      name: evt.tool || '?', detail, ok, args,
      file: args.path || fileFromToolDetail(detail),
      plus: stat[1] ? Number(stat[1]) : null,
      minus: stat[2] ? Number(stat[2]) : null,
      ms: evt.elapsed_ms || 0,
    });
    const div = document.createElement('div');
    div.className = 'step' + (ok ? '' : ' bad');
    div.innerHTML = '<span class="s-icon">' + toolIcon(evt.tool) + '</span>' +
      '<span class="s-text">' + esc(toolLabel(evt.tool, args, detail)) + '</span>' +
      '<span class="s-time">' + ((evt.elapsed_ms || 0) / 1000).toFixed(1) + 's</span>';
    steps.appendChild(div);
  } else if (evt.kind === 'subagent') {
    // 子 agent 动态：在 live 区显示开始/内部工具/完成（右侧面板另有完整记录）
    const div = document.createElement('div');
    div.className = 'step';
    if (evt.phase === 'start') {
      div.innerHTML = '<span class="s-icon">🤖</span><span class="s-text">子 agent 开始：' +
        esc(String(evt.task || '').slice(0, 60)) + '</span>';
    } else if (evt.phase === 'tool') {
      div.innerHTML = '<span class="s-icon">🤖</span><span class="s-text">子 agent 调用 ' +
        esc(String(evt.tool || '')) + '</span>';
    } else {
      div.innerHTML = '<span class="s-icon">🤖</span><span class="s-text">子 agent 完成（' +
        ((evt.elapsed_ms || 0) / 1000).toFixed(1) + 's' +
        (evt.steps && evt.steps.length ? ' · ' + evt.steps.length + ' 步' : '') +
        '）</span>';
    }
    steps.appendChild(div);
  }
  const log = $('log');
  if (nearBottom(log)) log.scrollTop = log.scrollHeight;
}

function showLiveBlock() {
  removeLiveBlock();
  liveLlmCount = 0;
  liveTools = [];
  const div = document.createElement('div');
  div.className = 'msg bot';
  div.innerHTML = '<div class="steps"></div><span class="thinking"><i></i><i></i><i></i></span>';
  $('thread').appendChild(div);
  liveBlock = div;
  $('log').scrollTop = $('log').scrollHeight;
}
function removeLiveBlock() {
  if (streamFrame) { cancelAnimationFrame(streamFrame); streamFrame = 0; }
  lastRounds = liveLlmCount;  // 供 add() 在「已深度思考」标题里显示轮数
  if (liveBlock) { liveBlock.remove(); liveBlock = null; }
  streamEl = null; streamBuf = '';
}

/* 本轮工具调用（流式期间累积，收尾时写入正式回复正文） */
let liveTools = [];

/* ---------- 流式输出 ---------- */
let streamEl = null;   // liveBlock 里承载增量的容器
let streamBuf = '';    // 已累积的全文（每帧整体重绘，避免增量拼接错位）
let streamFrame = 0;   // 待执行的 rAF 渲染帧（0 = 无排队）

/* 全局错误捕获：任何未捕获异常都记录到 window.__errs 并弹 toast——
   界面「莫名空白」时用户能直接看到具体错误，而不是无从排查 */
window.__errs = [];
window.onerror = function (msg, src, line, col) {
  window.__errs.push(String(msg) + ' @' + (src || '?') + ':' + (line || 0));
  try { toast('脚本错误: ' + msg, true); } catch (e2) { /* 忽略 */ }
  return false;
};

window.onStreamEvent = function (evt) {
  // 多会话并行：非当前会话的增量不渲染（后台继续收，done 时只提示）
  if (evt.session && evt.session !== currentSession && evt.kind !== 'done') return;
  if (evt.kind === 'delta') {
    if (!liveBlock) return;
    if (stopReq.has(evt.session)) return;   // 用户已停止：丢弃残余 delta
    if (!streamEl) {
      streamEl = document.createElement('div');
      streamEl.className = 'stream-body live';
      liveBlock.insertBefore(streamEl, liveBlock.querySelector('.thinking'));
      const dots = liveBlock.querySelector('.thinking');
      if (dots) dots.style.display = 'none';
    }
    streamBuf += (evt.text || '');
    // rAF 批量渲染：每个动画帧最多整体重绘一次（Markdown 渲染 + 吸底滚动），
    // 逐 token 的 evaluate_js 只做字符串拼接，不再每次都触发 layout
    if (!streamFrame) {
      streamFrame = requestAnimationFrame(() => {
        streamFrame = 0;
        if (!streamEl || !liveBlock) return;
        const log = $('log');
        const stick = nearBottom(log);
        streamEl.innerHTML = md(streamBuf);  // 流式期间也走 Markdown 渲染
        if (stick) log.scrollTop = log.scrollHeight;
      });
    }
    return;
  }
  if (evt.kind === 'done') finishStream(evt);
};

let roundStart = 0;   // 本轮对话开始时刻（算 tok/s 用）
let lastSpeed = 0;    // 上一轮生成速度 tokens/s（0=未知）

function roundSpeed(usage) {
  const c = Number((usage || {}).completion_tokens) || 0;
  const secs = roundStart ? (Date.now() - roundStart) / 1000 : 0;
  return (c && secs > 0.3) ? Math.max(1, Math.round(c / secs)) : 0;
}

function finishStream(evt) {
  busySessions.delete(evt.session);
  stopReq.delete(evt.session);
  // 非当前会话的完成：不渲染到当前线程，只提示 + 刷新侧栏
  if (evt.session && evt.session !== currentSession) {
    updateSendState();
    refreshSidebar();
    toast('会话已回复：' + (window.sessionNames && window.sessionNames[evt.session]
      || evt.session));
    flushQueue(evt.session);  // 后台会话的排队消息照常续跑
    return;
  }
  removeLiveBlock();
  setBusy(false);
  $('input').focus();
  if (!evt.ok) { add(evt.error || '未知错误', 'bot error'); flushQueue(evt.session); return; }
  lastSpeed = roundSpeed(evt.usage);
  const meta = ['模型: ' + (evt.model || '?')];
  for (const call of (evt.tool_calls || [])) meta.push(call.name);
  // 空回复兜底：模型多轮工具后没给文字总结时，明确说明而不是显示「（空回复）」
  let reply = (evt.reply || '').trim();
  if (evt.cancelled) {
    // 用户主动停止：保留已生成的部分内容 + 明确标记；后台会话照常续跑队列
    reply = (reply || '') + '\n\n（已停止生成。内容未保存到会话历史，可重新提问。）';
  } else if (!reply) {
    reply = liveTools.length
      ? '（本轮模型未返回文字总结。以下是执行的 ' + liveTools.length +
        ' 步操作；需要说明可再发一句「总结一下刚才做的事」。）'
      : '（模型没有返回内容。可能是请求被中断或连续工具调用达到上限，重发一次即可。）';
  }
  add(reply, 'bot', meta, evt.reasoning || '', undefined, liveTools.slice());
  liveTools = [];
  currentSession = evt.session || currentSession;
  refreshSidebar();
  refreshStatus();
  if (evt.budget_warning) toast(evt.budget_warning, true);
  notifyDone();
  if (curPanel === 'sub') loadSubagents();  // 子 agent 面板开着时刷新记录
  flushQueue(evt.session);  // 本轮结束 → 自动发送排队消息
}

/* ---------- 发送 ---------- */
// 按会话记录忙状态：一个会话在回答时，其他会话仍可发送（后端并行支持）
const busySessions = new Set();
// 每会话的消息排队：回答中仍然可以输入，本轮结束后自动依次发出
const queueBySession = new Map();
function queueDepth(sid) { return (queueBySession.get(sid) || []).length; }
function renderQueueBadge() {
  const el = $('queueBadge');
  if (!el) return;
  const n = queueDepth(currentSession);
  el.textContent = n ? ('排队 ' + n) : '';
  el.style.display = n ? '' : 'none';
  el.title = n ? ('本轮回答完成后将自动依次发送这 ' + n + ' 条消息') : '';
}
function updateSendState() {
  const busy = busySessions.has(currentSession);
  // 忙时按钮保持可点：此时发送 = 排队（本轮完成后自动发出）
  $('send').disabled = false;
  $('send').classList.toggle('busy', busy);
  // 回答中显示停止按钮（仅流式可真中断；整段路径点了也只在步骤间生效）
  $('stopBtn').style.display = busy ? '' : 'none';
  renderQueueBadge();
}
/* ---------- 停止生成 ---------- */
const stopReq = new Set();   // 已请求停止的会话（前端丢弃停止后的残余 delta）
async function stopStream() {
  const sid = currentSession;
  if (!busySessions.has(sid)) return;
  stopReq.add(sid);
  try { await api().stop_stream(sid); } catch (e) { /* 后端会在收尾时兜底 */ }
}
function setBusy(busy) {
  if (busy) busySessions.add(currentSession);
  else busySessions.delete(currentSession);
  updateSendState();
}

// 队列自动发送：本轮完成后取该会话队首消息续跑（支持后台会话）
async function flushQueue(sid) {
  const q = queueBySession.get(sid);
  if (!q || !q.length) { if (sid === currentSession) renderQueueBadge(); return; }
  const item = q.shift();
  if (q.length) queueBySession.set(sid, q);
  else queueBySession.delete(sid);
  if (sid === currentSession) renderQueueBadge();
  await send({ text: item.text, images: item.images, session: sid });
}

async function send(opts) {
  // opts: {text, images, session} —— 队列自动发送走这里（可指定会话，不碰输入框）
  const o = opts || {};
  const targetSession = o.session || currentSession;
  const isCurrent = targetSession === currentSession;
  let text, images;
  if (typeof o.text === 'string') {
    text = o.text;
    images = o.images || [];
  } else {
    const input = $('input');
    text = input.value.trim();
    if (!text && !pendingImages.length) return;
    images = pendingImages.slice();
    pendingImages = []; renderImgChips();
    if (isCurrent) { input.value = ''; input.style.height = 'auto'; }
  }
  // @引用展开：消息里的 @路径 替换为原文内容块（排队入队的也展开，保证语义一致）
  if (text) text = await expandAtRefs(text);
  // 该会话正在回答 → 入队，等本轮结束自动发出
  if (busySessions.has(targetSession)) {
    const q = queueBySession.get(targetSession) || [];
    q.push({ text: text || '（图片）', images: images });
    queueBySession.set(targetSession, q);
    if (isCurrent) {
      renderQueueBadge();
      toast('本轮回答中，已排队 ' + q.length + ' 条；完成后自动发送');
    }
    return;
  }
  if (isCurrent) {
    add(text || '（图片）', 'user', images.length ? images.map(p => '🖼 ' + imgLabel(p)) : null);
    showLiveBlock();
  }
  busySessions.add(targetSession);
  updateSendState();
  roundStart = Date.now();

  // 流式优先：模型支持 chat_stream 时逐字渲染（delta/done 事件经 onStreamEvent 推回）
  try {
    const cs = await api().can_stream();
    if (cs && cs.stream) {
      const res = await api().chat_stream(text, targetSession, images.length ? images : null);
      if (res.ok) {
        if (isCurrent) currentSession = res.session || targetSession;
        return;
      }
      // 流式派发失败（如已有对话在进行）→ 回退整段
      if (isCurrent) removeLiveBlock();
    }
  } catch (e) { /* 探测失败 → 回退整段 */ }

  const res = await api().chat(text, targetSession, images.length ? images : null);
  if (isCurrent) removeLiveBlock();
  busySessions.delete(targetSession);
  updateSendState();
  if (isCurrent) $('input').focus();
  if (!res.ok) {
    if (isCurrent) add(res.error || '未知错误', 'bot error');
    else toast('会话回复失败：' + (res.error || '未知错误'), true);
    await flushQueue(targetSession);
    return;
  }
  lastSpeed = roundSpeed(res.usage);
  const meta = ['模型: ' + (res.model || '?')];
  for (const call of (res.tool_calls || [])) meta.push(call.name);
  let reply = (res.reply || '').trim();
  if (!reply) {
    reply = (res.tool_calls || []).length
      ? '（本轮模型未返回文字总结；共调用 ' + res.tool_calls.length + ' 次工具。）'
      : '（模型没有返回内容。可能是请求被中断，重发一次即可。）';
  }
  if (isCurrent) add(reply, 'bot', meta, res.reasoning || '');
  if (res.budget_warning) toast(res.budget_warning, true);
  if (isCurrent) currentSession = res.session || currentSession;
  refreshSidebar();
  refreshStatus();
  notifyDone();
  await flushQueue(targetSession);
}

/* ---------- 图片附加（功能7） ---------- */
let pendingImages = [];
function imgLabel(p) {
  // data URL（剪贴板粘贴的截图）没有文件名，显示固定标签而不是一长串 base64
  return String(p).startsWith('data:') ? '剪贴板图片' : String(p).split(/[\\\\/]/).pop();
}
function renderImgChips() {
  const box = $('imgChips');
  box.innerHTML = pendingImages.map((p, i) =>
    '<span class="img-chip" title="点击预览" style="cursor:pointer" ' +
    'onclick="previewImage(pendingImages[' + i + '])">🖼 ' + esc(imgLabel(p)) +
    ' <a style="cursor:pointer" onclick="event.stopPropagation();removeImage(' + i + ')">✕</a></span>').join('');
  box.style.display = pendingImages.length ? 'flex' : 'none';
}
function addImagePath(p) {
  if (!p || pendingImages.includes(p)) return;
  pendingImages.push(p);
  renderImgChips();
}
function removeImage(i) { pendingImages.splice(i, 1); renderImgChips(); }

/* ---------- 图片预览（lightbox：data URL 直显，本地路径走 image_preview） ---------- */
async function previewImage(p) {
  const img = $('lightboxImg');
  if (String(p).startsWith('data:')) {
    img.src = p;
  } else {
    const res = await api().image_preview(p);
    if (!res.ok) { toast(res.error || '无法预览该图片', true); return; }
    img.src = res.data;
  }
  $('lightbox').classList.add('on');
}
$('lightbox').onclick = () => $('lightbox').classList.remove('on');
async function attachImage() {
  const res = await api().pick_image();
  if (!res.ok) { toast(res.error || '无法打开文件对话框', true); return; }
  for (const p of (res.paths || [])) {
    const chk = await api().check_image(p);
    if (chk.ok) addImagePath(chk.path);
    else toast(chk.error, true);
  }
}
$('input').addEventListener('paste', async (e) => {
  const cd = e.clipboardData || window.clipboardData;
  if (!cd) return;
  // 1) 剪贴板里是图片本体（截图 / 复制的图片）→ 读成 data URL 直接附加。
  //    safe_image 对 data: URL 原样放行，无需落临时文件。
  const imgItem = [...(cd.items || [])].find(i => i.type && i.type.startsWith('image/'));
  if (imgItem) {
    e.preventDefault();
    const blob = imgItem.getAsFile();
    if (!blob) return;
    if (blob.size > 8 * 1024 * 1024) {
      toast('剪贴板图片超过 8MB，请先保存为文件后用 📎 附加。', true);
      return;
    }
    const reader = new FileReader();
    reader.onload = () => addImagePath(String(reader.result));
    reader.readAsDataURL(blob);
    return;
  }
  // 2) 文本粘贴：图片路径 → 附加；外部工具（如 WorkBuddy）的 @image#N: 引用
  //    是内部格式、拿不到图片本体 → 明确提示，而不是把占位文本留在输入框
  const text = cd.getData('text');
  if (!text) return;
  const t = text.trim();
  const wbRef = t.match(/^@image#\d+:(.+)$/);
  if (wbRef) {
    e.preventDefault();
    add('检测到剪贴板图片引用「' + esc(wbRef[1]) + '」——这是外部聊天工具的内部格式，' +
        '拿不到图片本体。请直接 Ctrl+V 粘贴图片，或用 📎 选择文件。', 'bot');
    return;
  }
  if (!/^[^\r\n]+\.(png|jpe?g|gif|webp)$/i.test(t)) return;
  const chk = await api().check_image(t);
  if (chk.ok) { e.preventDefault(); addImagePath(chk.path); }
});

/* ---------- @文件引用：输入 @ 触发工作区文件补全，发送时把引用文件内容附进消息 ---------- */
const atState = { active: false, items: [], index: 0, query: '', tokenStart: -1 };
let wsFileCache = { ts: 0, files: [] };

async function wsFilesFresh() {
  if (Date.now() - wsFileCache.ts > 30000) {
    try {
      const r = await api().ws_files(800);
      wsFileCache = { ts: Date.now(), files: (r && r.files) || [] };
    } catch (e) { wsFileCache = { ts: Date.now(), files: [] }; }
  }
  return wsFileCache.files;
}
function atOnInput() {
  const el = $('input');
  const upto = el.value.slice(0, el.selectionStart || 0);
  const m = upto.match(/(^|[\s（(【"])@([^\s@]*)$/);
  if (!m) { atClose(); return; }
  atState.query = m[2];
  atState.tokenStart = (el.selectionStart || 0) - m[2].length;
  atOpen();
}
async function atOpen() {
  const files = await wsFilesFresh();
  const q = (atState.query || '').toLowerCase();
  atState.items = files.filter(f => f.toLowerCase().includes(q)).slice(0, 8);
  if (!atState.items.length) { atClose(); return; }
  atState.active = true;
  atState.index = Math.min(atState.index, atState.items.length - 1);
  renderAtMenu();
}
function renderAtMenu() {
  const menu = $('atMenu');
  if (!menu) return;
  menu.innerHTML = '';
  atState.items.forEach((f, i) => {
    const b = document.createElement('div');
    b.className = 'at-item' + (i === atState.index ? ' active' : '');
    b.textContent = '@' + f;
    b.title = f;
    b.onmousedown = (e) => { e.preventDefault(); atPick(i); };
    menu.appendChild(b);
  });
  menu.style.display = 'block';
}
function atClose() {
  atState.active = false;
  const menu = $('atMenu');
  if (menu) { menu.style.display = 'none'; menu.innerHTML = ''; }
}
function atPick(i) {
  const el = $('input');
  const f = atState.items[i];
  if (!f) { atClose(); return; }
  const pos = el.selectionStart || 0;
  const before = el.value.slice(0, atState.tokenStart);
  const after = el.value.slice(pos).replace(/^\s+/, '');
  const ins = '@' + f + ' ';
  el.value = before + ins + after;
  atClose();
  el.focus();
  const caret = (before + ins).length;
  el.setSelectionRange(caret, caret);
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 180) + 'px';
}
/* 发送前把 @路径 展开成文件内容块（最多 3 个、单个 12k 字符，后端有监狱） */
async function expandAtRefs(text) {
  const refs = [...new Set((String(text).match(/@([^\s@,，。;；)）\]]+)/g) || [])
    .map(s => s.slice(1)))];
  if (!refs.length) return text;
  let extra = '';
  let used = 0;
  for (const ref of refs) {
    if (used >= 3) break;
    try {
      const r = await api().ws_file_text(ref, 12000);
      if (r && r.ok) { used++; extra += '\n\n[引用文件 ' + ref + ']\n```\n' + r.text + '\n```'; }
    } catch (e) { /* 引用失效就跳过，消息照发 */ }
  }
  return used ? (text + extra) : text;
}

$('input').addEventListener('input', () => { atOnInput(); });
$('input').addEventListener('keydown', (e) => {
  if (atState.active) {
    if (e.key === 'ArrowDown') { e.preventDefault(); atState.index = (atState.index + 1) % atState.items.length; renderAtMenu(); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); atState.index = (atState.index - 1 + atState.items.length) % atState.items.length; renderAtMenu(); return; }
    if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); atPick(atState.index); return; }
    if (e.key === 'Escape') { e.preventDefault(); atClose(); return; }
  }
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});

/* ---------- 全局快捷键（Ctrl+N 新会话 / Ctrl+F 搜索 / Ctrl+, 设置 / Ctrl+B 侧栏 / Ctrl+J 面板） ---------- */
document.addEventListener('keydown', (e) => {
  const mod = e.ctrlKey || e.metaKey;
  if (!mod || e.altKey) return;
  const k = (e.key || '').toLowerCase();
  if (k === 'n') { e.preventDefault(); newSessionFlow(); }
  else if (k === 'f') { e.preventDefault(); searchFlow(); }
  else if (k === ',') { e.preventDefault(); openSettings(); }
  else if (k === 'b') { e.preventDefault(); toggleSide(); }
  else if (k === 'j') { e.preventDefault(); togglePanel(); }
});
$('input').addEventListener('input', function () {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 180) + 'px';
});
$('sessionFilter').addEventListener('input', () => refreshSidebar());
$('modelSel').onchange = async () => {
  const res = await api().switch_model($('modelSel').value);
  if (res.ok) { currentModel = res.model; toast('已切换模型: ' + res.model); }
  else toast(res.error || res.message, true);
  refreshStatus();
};
$('thinkSel').onchange = async () => {
  const res = await api().set_thinking($('thinkSel').value);
  if (res.ok) {
    updateRegenVisibility();
    toast(res.thinking_level === 'off' ? '已关闭思考' : '思考级别: ' + res.thinking_level);
  }
};

/* ---------- 计划模式（📋 按钮） ---------- */
async function togglePlanMode() {
  const on = !$('planBtn').classList.contains('on');
  const res = await api().set_plan_mode(on);
  if (!res.ok) { toast(res.error || '切换失败', true); return; }
  $('planBtn').classList.toggle('on', !!res.plan_mode);
  toast(res.message);
}

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
  document.querySelectorAll('.set-nav-item').forEach(b =>
    b.classList.toggle('active', b.dataset.set === name));
  ['models', 'plugins', 'skills', 'market', 'mcp', 'appearance', 'general', 'usage']
    .forEach(n => {
      const el = $('tab-' + n);
      if (el) el.style.display = n === name ? '' : 'none';
    });
  if (name === 'market' && !marketLoaded) loadMarket();
  if (name === 'skills') loadSkills();
  if (name === 'mcp') loadMcp();
  if (name === 'appearance') renderAppearance();
  if (name === 'general') renderGeneral();
  if (name === 'usage') loadUsageTab();
}
document.addEventListener('keydown', (e) => {
  // 权限确认框可见时 Esc = 明确拒绝（H-05：此前只隐藏不回结果，Python 侧空等超时）
  if (e.key === 'Escape') {
    if ($('dlgOverlay').style.display !== 'none') _closeDialog(false);
    closeSettings();
    $('dlgOverlay').style.display = 'none';
  }
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
      <div class="model-presets">
        <label>提供商预设</label>
        <select onchange="applyPreset(${i}, this.value); this.selectedIndex = 0;">
          <option value="">— 选择预设，自动填地址与模型，只需再填 API Key —</option>
          ${MODEL_PRESETS.map((p, pi) => '<option value="' + pi + '">' + esc(p.n) + '</option>').join('')}
        </select>
      </div>
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

/* 内置提供商预设：选一条自动填 base_url + 模型 ID，只需再填 API Key */
const MODEL_PRESETS = [
  { n: 'DeepSeek 官方', base: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  { n: 'DeepSeek 推理（R1）', base: 'https://api.deepseek.com/v1', model: 'deepseek-reasoner' },
  { n: 'Moonshot Kimi', base: 'https://api.moonshot.cn/v1', model: 'kimi-k2-turbo-preview' },
  { n: '智谱 GLM', base: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4.6' },
  { n: '阿里百炼 Qwen', base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' },
  { n: 'SiliconFlow 硅基流动', base: 'https://api.siliconflow.cn/v1', model: 'deepseek-ai/DeepSeek-V3' },
  { n: 'OpenAI 官方', base: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
  { n: 'Anthropic 官方', base: 'https://api.anthropic.com', model: 'claude-sonnet-4-5', provider: 'anthropic' },
];

function applyPreset(i, val) {
  const p = MODEL_PRESETS[Number(val)];
  if (!p || !editing.models[i]) return;
  editing.models[i].base_url = p.base;
  editing.models[i].model = p.model;
  if (p.provider) editing.models[i].provider = p.provider;
  if (!editing.models[i].name) editing.models[i].name = p.n;
  renderCards();
  const note = $('saveNote');
  note.className = '';
  note.textContent = '已填入「' + p.n + '」——再填 API Key 后点保存即可';
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

/* ---------- MCP 服务器 ---------- */
let editingMcp = [];

async function loadMcp() {
  const s = await api().mcp_servers();
  editingMcp = (s.servers || []).map(x => ({
    name: x.name || '', type: x.type || 'stdio', url: x.url || '',
    command: x.command || '', args: (x.args || []).join(' '),
    headers: Object.keys(x.headers || {}).length ? JSON.stringify(x.headers) : '',
    env: Object.keys(x.env || {}).length ? JSON.stringify(x.env) : '',
  }));
  if (!editingMcp.length) addMcpEntry();
  renderMcpCards();
  $('mcpSaveNote').textContent = '';
}
function addMcpEntry() {
  editingMcp.push({ name: '', type: 'stdio', url: '', command: '', args: '', headers: '', env: '' });
}
function addMcpCard() { addMcpEntry(); renderMcpCards(); }

function renderMcpCards() {
  const box = $('mcpCards');
  box.innerHTML = '';
  editingMcp.forEach((m, i) => {
    const card = document.createElement('div');
    card.className = 'model-card';
    const remote = m.type !== 'stdio';
    card.innerHTML = `
      <div class="model-grid">
        <div class="field"><label>名称</label>
          <input value="${esc(m.name)}" oninput="editingMcp[${i}].name=this.value"></div>
        <div class="field"><label>传输类型</label>
          <select onchange="editingMcp[${i}].type=this.value;renderMcpCards()">
            <option value="stdio"${!remote ? ' selected' : ''}>stdio（本地命令）</option>
            <option value="http"${m.type === 'http' ? ' selected' : ''}>http（远程）</option>
            <option value="sse"${m.type === 'sse' ? ' selected' : ''}>sse（远程）</option>
          </select></div>
        ${remote
          ? `<div class="field"><label>URL</label>
               <input value="${esc(m.url)}" oninput="editingMcp[${i}].url=this.value"></div>`
          : `<div class="field"><label>启动命令</label>
               <input value="${esc(m.command)}" oninput="editingMcp[${i}].command=this.value"></div>`}
      </div>
      <div class="model-grid2">
        ${remote ? '' : `<div class="field"><label>参数（空格分隔）</label>
          <input value="${esc(m.args)}" oninput="editingMcp[${i}].args=this.value"></div>`}
        <div class="field"><label>HEADERS（JSON，可选）</label>
          <input type="password" value="${esc(m.headers)}" oninput="editingMcp[${i}].headers=this.value"></div>
        <div class="field"><label>ENV（JSON，可选）</label>
          <input type="password" value="${esc(m.env)}" oninput="editingMcp[${i}].env=this.value"></div>
        <button class="icon-btn" onclick="editingMcp.splice(${i},1);renderMcpCards()">移除</button>
      </div>`;
    box.appendChild(card);
  });
}

function parseJsonField(text, label, problems) {
  const t = (text || '').trim();
  if (!t) return {};
  try {
    const v = JSON.parse(t);
    if (v && typeof v === 'object' && !Array.isArray(v)) return v;
    problems.push(label + ' 必须是 JSON 对象');
  } catch (e) { problems.push(label + ' 不是合法 JSON'); }
  return {};
}

async function saveMcp() {
  const problems = [];
  const servers = editingMcp.map(m => ({
    name: m.name, type: m.type, url: m.url, command: m.command, args: m.args,
    headers: parseJsonField(m.headers, (m.name || '未命名') + ' 的 HEADERS', problems),
    env: parseJsonField(m.env, (m.name || '未命名') + ' 的 ENV', problems),
  }));
  const note = $('mcpSaveNote');
  if (problems.length) {
    note.className = 'note-err';
    note.textContent = problems[0];
    return;
  }
  const res = await api().save_mcp(servers);
  note.className = res.ok ? 'note-ok' : 'note-err';
  note.textContent = res.ok
    ? ('已保存 ' + res.count + ' 个服务器' +
       (res.reload_error ? '；重连失败: ' + res.reload_error : '；已重连'))
    : ('保存失败: ' + (res.error || '未知错误'));
  if (res.ok) loadMcp();
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
// 统一切换入口：侧栏工作区点击 / composer 下拉 / 原生对话框都走这里，
// 保证 composer 上方显示与右侧文件树同步刷新
async function switchWorkspace(path) {
  const r = await api().set_workspace(path);
  if (r.ok) {
    wsPath = '';            // 右侧文件树回到新工作区根目录
    await refreshStatus();  // 更新 composer 上方的工作区名
    refreshSidebar();       // 侧栏分组与当前高亮
    if (document.querySelector('#rp-ws.active')) loadWsTree();  // 右侧文件树开着才重载
  }
  return r;
}

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
      await switchWorkspace(ws.path);
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
    await switchWorkspace(res.workspace);  // 后端已切好，这里只做联动刷新
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
}

/* ---------- 标签栏滚轮横滚：标签多放不下时，滚轮直接左右移动标签条 ---------- */
(function initTabsWheel() {
  const bar = document.querySelector('.rp-tabs');
  if (!bar) return;
  bar.addEventListener('wheel', (e) => {
    // 纵向滚轮转成横向滚动（按住 Shift 时浏览器原生横滚，去重避免双倍）
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      e.preventDefault();
      bar.scrollLeft += e.deltaY;
    }
  }, { passive: false });
})();

/* ---------- 导出当前会话 ---------- */
async function exportCurrent() {
  const res = await api().export_session(currentSession);
  const hint = $('statusHint');
  if (res.ok) {
    hint.textContent = '已导出: ' + res.path;
    setTimeout(() => { hint.textContent = ''; }, 6000);
  } else {
    toast('导出失败: ' + (res.error || '未知错误'), true);
  }
}
function switchPanel(name) {
  curPanel = name;
  document.querySelectorAll('.rp-tab').forEach(t => t.classList.toggle('active', t.dataset.rp === name));
  document.querySelectorAll('.rp-body').forEach(b => b.classList.toggle('active', b.id === 'rp-' + name));
  if (name === 'ws') loadWsTree();
  if (name === 'kb') loadKnowledge();
  if (name === 'todo') loadTodoPanel();
  if (name === 'sched') loadSchedules();
  if (name === 'mem') loadMemory();
  if (name === 'usage') loadUsageChart();
  if (name === 'review') loadReview();
  if (name === 'sub') loadSubagents();
}

/* ---------- 右侧标签页显隐（点 ✕ 隐藏，「＋」处找回） ---------- */
let rpHidden = [];
const TAB_LABELS = { aux: '💬 辅助', ws: '📁 工作区', sub: '🤖 子agent', kb: '📚 知识库',
                     todo: '✅ 任务', sched: '⏰ 定时', mem: '🧠 记忆', usage: '📈 用量',
                     term: '⌨ 终端', browser: '🌐 浏览器', review: '🔍 审查' };

function applyTabVisibility() {
  document.querySelectorAll('.rp-tab[data-rp]').forEach(t => {
    t.style.display = rpHidden.includes(t.dataset.rp) ? 'none' : '';
  });
  // ＋ 按钮无隐藏项时呈禁用态
  const plus = document.querySelector('.rp-plus');
  if (plus) plus.classList.toggle('dim', !rpHidden.length);
  // 当前激活的标签被隐藏 → 切到第一个可见标签
  const active = document.querySelector('.rp-tab.active');
  if (active && rpHidden.includes(active.dataset.rp)) {
    const first = document.querySelector('.rp-tab[data-rp]:not([style*="none"])');
    if (first) switchPanel(first.dataset.rp);
  }
}
function hideTab(name) {
  if (!rpHidden.includes(name)) rpHidden.push(name);
  saveUiPrefs();
  applyTabVisibility();
}
async function showTabMenu() {
  // 在标签栏内弹出下拉菜单（不再用全屏对话框）
  const menu = $('tabMenu');
  const hidden = Object.keys(TAB_LABELS).filter(n => rpHidden.includes(n));
  if (!hidden.length) return;  // ＋ 按钮此时呈半透明禁用态
  if (menu.classList.contains('open')) { menu.classList.remove('open'); return; }
  const plus = document.querySelector('.rp-plus');
  if (plus) {
    const r = plus.getBoundingClientRect();
    menu.style.left = Math.max(4, r.left - 140) + 'px';
    menu.style.top = (r.bottom + 6) + 'px';
  }
  menu.innerHTML = '';
  for (const n of hidden) {
    const btn = document.createElement('button');
    btn.innerHTML = '<span>' + esc(TAB_LABELS[n] || n) + '</span>';
    btn.title = '恢复显示此标签页';
    btn.onclick = (e) => {
      e.stopPropagation();
      menu.classList.remove('open');
      rpHidden = rpHidden.filter(x => x !== n);
      saveUiPrefs();
      applyTabVisibility();
      switchPanel(n);
    };
    menu.appendChild(btn);
  }
  menu.classList.add('open');
}
/* 标签页顺序：支持拖拽重排 / 右键「左移 / 右移 / 隐藏」，随界面偏好持久化 */
const RP_TAB_ORDER_DEFAULT = ['aux', 'ws', 'sub', 'todo', 'sched', 'mem', 'usage', 'kb', 'term', 'browser', 'review'];
let rpTabOrder = RP_TAB_ORDER_DEFAULT.slice();
let curPanel = 'aux';

function renderTabs() {
  const box = $('rpTabsBox');
  if (!box) return;
  box.innerHTML = '';
  rpTabOrder.forEach((name, idx) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'rp-tab' + (name === curPanel ? ' active' : '');
    b.dataset.rp = name;
    b.textContent = TAB_LABELS[name] || name;
    b.onclick = () => switchPanel(name);
    // 鼠标自实现拖拽换位（同会话行）：不用 HTML5 原生拖拽，避免系统拖影白框
    b.onmousedown = (e) => {
      if (e.button !== 0) return;
      const startX = e.clientX;
      let moved = false;
      const move = (ev) => {
        if (!moved && Math.abs(ev.clientX - startX) < 5) return;
        moved = true;
        ev.preventDefault();
        const t = document.elementFromPoint(ev.clientX, ev.clientY);
        const target = t && t.closest ? t.closest('.rp-tab[data-rp]') : null;
        if (!target || target.dataset.rp === name) return;
        const from = rpTabOrder.indexOf(name);
        const to = rpTabOrder.indexOf(target.dataset.rp);
        if (from < 0 || to < 0 || from === to) return;
        const movedTab = rpTabOrder.splice(from, 1)[0];
        rpTabOrder.splice(to, 0, movedTab);
        saveUiPrefs();
        renderTabs();  // 旧节点随重建销毁；监听在 document 上，继续命中新节点
      };
      const up = () => {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
      };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    };
    b.oncontextmenu = (e) => {
      e.preventDefault();
      tabMoveMenu(name, e.clientX, e.clientY);
    };
    const x = document.createElement('span');
    x.className = 'tab-x';
    x.textContent = '✕';
    x.title = '隐藏此标签页（右侧「＋」可找回）';
    x.onclick = (e) => { e.stopPropagation(); hideTab(name); };
    b.appendChild(x);
    box.appendChild(b);
  });
}

function tabMoveMenu(name, x, y) {
  // 右键标签页：左移 / 右移 / 隐藏
  const menu = $('tabMenu');
  menu.innerHTML = '';
  const idx = rpTabOrder.indexOf(name);
  const addItem = (label, enabled, fn) => {
    const btn = document.createElement('button');
    btn.innerHTML = '<span>' + esc(label) + '</span>';
    if (!enabled) btn.style.opacity = '.4';
    else btn.onclick = (e) => { e.stopPropagation(); menu.classList.remove('open'); fn(); };
    menu.appendChild(btn);
  };
  addItem('◀ 左移', idx > 0, () => {
    [rpTabOrder[idx - 1], rpTabOrder[idx]] = [rpTabOrder[idx], rpTabOrder[idx - 1]];
    saveUiPrefs(); renderTabs(); applyTabVisibility();
  });
  addItem('▶ 右移', idx < rpTabOrder.length - 1, () => {
    [rpTabOrder[idx + 1], rpTabOrder[idx]] = [rpTabOrder[idx], rpTabOrder[idx + 1]];
    saveUiPrefs(); renderTabs(); applyTabVisibility();
  });
  addItem('✕ 隐藏此标签页', true, () => hideTab(name));
  menu.style.left = Math.max(4, x - 30) + 'px';
  menu.style.top = (y + 10) + 'px';
  menu.classList.add('open');
}

(function initTabs() {
  const tabs = document.querySelector('.rp-tabs');
  if (!tabs) return;
  renderTabs();
  const plus = document.createElement('button');
  plus.type = 'button';
  plus.className = 'rp-tab rp-plus';
  plus.textContent = '＋';
  plus.title = '找回隐藏的标签页';
  plus.onclick = (e) => { e.stopPropagation(); showTabMenu(); };
  const close = tabs.querySelector('.rp-close');
  if (close) tabs.insertBefore(plus, close); else tabs.appendChild(plus);
  applyTabVisibility();
})();
// 点面板其它位置时收起「找回标签页」下拉
document.addEventListener('click', (e) => {
  if (!e.target.closest('#tabMenu')) {
    const menu = $('tabMenu');
    if (menu) menu.classList.remove('open');
  }
});

/* ---------- 任务面板（工作区 TODO.md 渲染） ---------- */
const TODO_MARKS = [
  [/^- \[ \]\s*/, 't-pending', '<span class="t-box"></span>'],
  [/^- \[~\]\s*/, 't-doing', '<span class="t-box t-doing-box">…</span>'],
  [/^- \[x\]\s*/, 't-done', '<span class="t-box t-done-box">✓</span>'],
  [/^- \[-\]\s*/, 't-done', '<span class="t-box">—</span>'],
];

async function loadTodoPanel() {
  const pane = $('todoPane');
  pane.innerHTML = '<div class="hint">加载中…</div>';
  const res = await api().todo_content();
  pane.innerHTML = '';
  if (!res.ok) {
    pane.innerHTML = '<div class="hint">' + esc(res.error || '加载失败') + '</div>';
    return;
  }
  if (!res.exists || !(res.content || '').trim()) {
    pane.innerHTML = '<div class="hint">工作区还没有 TODO.md。<br>' +
      '对 agent 说「把工程目标拆成原子任务清单写入 TODO.md」即可自动创建；' +
      '长程任务会在每个新会话自动读取这里的未完成项。</div>';
    return;
  }
  for (const line of res.content.split('\n')) {
    const s = line.trim();
    if (!s) continue;
    const row = document.createElement('div');
    let matched = false;
    for (const [re, cls, box] of TODO_MARKS) {
      if (re.test(s)) {
        row.className = 'todo-row ' + cls;
        row.innerHTML = box + '<span class="t-name">' +
          esc(s.replace(re, '')) + '</span>';
        matched = true;
        break;
      }
    }
    if (!matched) {
      row.className = 'todo-hint';
      row.textContent = line;
    }
    pane.appendChild(row);
  }
}

/* ---------- 一键无状态重置（总结写回 TODO.md → 自动新会话） ---------- */
async function statelessReset() {
  const ok = await dialogConfirm('无状态重置',
    '将请 agent 把当前进展总结写回工作区 TODO.md，然后自动开始一个全新会话' +
    '（新会话会自动读取 TODO.md 与最近提交接续任务；旧会话保留可回看）。继续？');
  if (!ok) return;
  showLiveBlock(); setBusy(true);
  const r = await api().stateless_reset();
  removeLiveBlock(); setBusy(false);
  if (!r.ok) {
    toast('无状态重置失败: ' + (r.error || '未知错误'), true);
    return;
  }
  toast('已写入 TODO.md：' + (r.note || '（空）'));
  await selectSession(r.session);
}

/* ---------- 长期记忆（🧠 记忆；profile/memory.md，构建 agent 时注入） ---------- */
async function loadMemory() {
  const r = await api().agent_memory();
  $('memText').value = r.ok ? (r.text || '') : ('读取失败: ' + (r.error || ''));
}
async function saveMemory() {
  const r = await api().save_agent_memory($('memText').value);
  if (r && r.ok) toast('记忆已保存，下一轮对话生效');
  else toast((r && r.error) || '保存失败', true);
}

/* ---------- profile 备份 / 恢复（通用 → 数据） ---------- */
async function backupProfile() {
  const r = await api().export_profile('');
  if (r && r.ok) toast('已备份 ' + r.files + ' 个文件 → ' + r.path);
  else toast((r && r.error) || '备份失败', true);
}
async function restoreProfile() {
  const path = await dialogPrompt('从备份恢复 —— 输入备份 zip 的完整路径', '');
  if (path === null || !String(path).trim()) return;
  const ok = await dialogConfirm('确认恢复',
    '将用备份覆盖当前 profile 的同名文件（配置先存为 config.pre-import.json）。继续？');
  if (!ok) return;
  const r = await api().import_profile(String(path).trim());
  if (r && r.ok) toast('已恢复 ' + r.restored + ' 个文件' + (r.skipped ? '，跳过 ' + r.skipped : ''));
  else toast((r && r.error) || '恢复失败', true);
}

/* ---------- 定时任务面板（⏰ 定时；与 scheduler 插件 / sha schedule 共用 tasks.json） ---------- */
async function loadSchedules() {
  const pane = $('schedPane');
  if (!pane) return;
  pane.innerHTML = '<div class="hint">加载中…</div>';
  const r = await api().schedules();
  pane.innerHTML = '';
  const tasks = (r && r.tasks) || [];
  if (!tasks.length) {
    pane.innerHTML = '<div class="hint">还没有定时任务。<br>' +
      '用 <b>sha schedule add &lt;名&gt; --every &lt;秒&gt; --prompt &lt;提示词&gt;</b> 创建，' +
      '或直接让 agent 帮你安排。到点会用独立 agent 执行提示词，结果写进 profile 的 scheduled/ 目录。</div>';
    return;
  }
  for (const t of tasks) {
    const row = document.createElement('div');
    row.className = 'sub-row open';
    const head = document.createElement('div');
    head.className = 'sub-head';
    const last = t.last_run ? new Date(t.last_run * 1000).toLocaleString('zh-CN') : '从未';
    head.innerHTML = '<span class="sub-badge ' + (t.enabled !== false ? 'ok' : 'bad') + '">' +
      (t.enabled !== false ? '▶' : '⏸') + '</span>' +
      '<span class="sub-task">' + esc(t.name || '') + '</span>' +
      '<span class="sub-time">每 ' + esc(String(t.every ?? '?')) + 's · 上次 ' + esc(last) + '</span>';
    head.title = t.prompt || '';
    head.onclick = () => row.classList.toggle('open');
    row.appendChild(head);
    const body = document.createElement('div');
    body.className = 'sub-body';
    const prompt = document.createElement('div');
    prompt.className = 'sub-out';
    prompt.textContent = t.prompt || '';
    const acts = document.createElement('div');
    acts.className = 'sched-acts';
    const toggle = document.createElement('button');
    toggle.className = 'mini-btn';
    toggle.textContent = t.enabled !== false ? '⏸ 暂停' : '▶ 启用';
    toggle.onclick = async (e) => {
      e.stopPropagation();
      const res = await api().schedule_toggle(t.name, t.enabled === false);
      if (res && res.ok) loadSchedules();
      else toast((res && res.error) || '操作失败', true);
    };
    const del = document.createElement('button');
    del.className = 'mini-btn';
    del.textContent = '🗑 移除';
    del.onclick = async (e) => {
      e.stopPropagation();
      const ok = await dialogConfirm('移除定时任务', '确定移除任务「' + (t.name || '') + '」？');
      if (!ok) return;
      const res = await api().schedule_remove(t.name);
      if (res && res.ok) loadSchedules();
      else toast((res && res.error) || '移除失败', true);
    };
    acts.append(toggle, del);
    body.append(prompt, acts);
    row.appendChild(body);
    pane.appendChild(row);
  }
}

/* ---------- 远程仓库配置与手动推送（审查面板；agent 无 push 能力） ---------- */
async function loadGitRemote() {
  const res = await api().git_remote_get();
  if (res.ok) $('gitRemoteUrl').value = res.url || '';
}

async function saveGitRemote() {
  const url = $('gitRemoteUrl').value.trim();
  if (!url) { toast('远程地址为空', true); return; }
  const res = await api().git_remote_set(url);
  const out = $('reviewOut');
  if (res.ok) {
    out.textContent = (res.action || '已保存') + ' origin → ' + res.url +
      '\n（提交请手动执行；推送点「⇅ 推送」）';
  } else {
    out.textContent = '保存失败: ' + (res.error || '未知错误');
  }
}

async function pushNow() {
  const ok = await dialogConfirm('推送到远程',
    '将执行 git push -u origin HEAD（把当前分支推送到已保存的 origin）。继续？');
  if (!ok) return;
  const out = $('reviewOut');
  out.textContent = '推送中…';
  const res = await api().git_push();
  out.textContent = res.ok ? (res.output || '推送完成')
                           : '推送失败:\n' + (res.output || res.error || '');
  refreshSidebar();
}

/* ---------- 侧栏宽度拖拽 ---------- */
(function initSideResize() {
  const grip = $('sideResize');
  const side = document.querySelector('aside');
  if (!grip || !side) return;
  let dragging = false, startX = 0, startW = 0;
  grip.addEventListener('mousedown', (e) => {
    dragging = true; startX = e.clientX;
    startW = side.getBoundingClientRect().width;
    grip.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const w = Math.max(180, Math.min(460, startW + (e.clientX - startX)));
    side.style.width = w + 'px';
    side.style.minWidth = w + 'px';
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    grip.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    saveUiPrefs();
  });
  const saved = parseInt(lsGet('sh_side_width') || '0', 10);
  if (saved >= 180 && saved <= 460) {
    side.style.width = saved + 'px';
    side.style.minWidth = saved + 'px';
  }
})();

/* ---------- 知识库面板 ---------- */
let kbNote = '';

/* 知识库试检索：不走对话，直接看命中片段与来源 */
async function kbQuery() {
  const input = $('kbqInput');
  const out = $('kbqOut');
  const q = (input.value || '').trim();
  if (!q) return;
  out.style.display = '';
  out.textContent = '检索中…';
  const r = await api().knowledge_query(q, 5);
  out.textContent = r.ok ? (r.output || '（无命中）') : ('错误: ' + (r.error || '未知错误'));
}

async function loadKnowledge() {
  const res = await api().knowledge_status();
  const pane = $('kbPane');
  pane.innerHTML = '';
  if (kbNote) {
    const note = document.createElement('div');
    note.className = 'hint';
    note.style.marginBottom = '8px';
    note.textContent = kbNote;
    pane.appendChild(note);
    kbNote = '';
  }
  if (!res.ok) {
    const err = document.createElement('div');
    err.className = 'hint';
    err.textContent = res.error || '知识库不可用';
    pane.appendChild(err);
    return;
  }
  const rows = [
    ['片段数', res.chunks === -1 ? '已入库（条数待重建后可知）' : String(res.chunks ?? 0)],
    ['向量后端', res.backend || 'numpy'],
    ['Embedding', res.embedding_model || '（模型默认）'],
    ['索引目录', res.dir || ''],
  ];
  for (const [k, v] of rows) {
    const div = document.createElement('div');
    div.className = 'ctx-row';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = k;
    const val = document.createElement('span');
    val.className = 'pct';
    val.textContent = v;
    val.title = v;
    div.appendChild(name);
    div.appendChild(val);
    pane.appendChild(div);
  }
  const hint = document.createElement('div');
  hint.className = 'hint';
  hint.style.marginTop = '10px';
  hint.textContent = 'agent 对话会自动用 search_knowledge 检索这里的内容；' +
    '也可以直接让它「把 xx 文件加入知识库」。只索引 md/txt/py/json/yaml/rst/csv/sahou 等文本文件。';
  pane.appendChild(hint);

  // 细粒度管理：按来源文件列出片段数，可单独删除某个来源
  const srcRes = await api().knowledge_sources();
  if (!srcRes.ok) return;
  const sources = srcRes.sources || [];
  const title = document.createElement('div');
  title.className = 'side-label';
  title.style.margin = '14px 0 4px';
  title.textContent = '已索引来源（' + sources.length + '）';
  pane.appendChild(title);
  if (!sources.length) {
    const empty = document.createElement('div');
    empty.className = 'hint';
    empty.textContent = '（还没有来源记录；新索引的文件会出现在这里）';
    pane.appendChild(empty);
    return;
  }
  for (const s of sources) {
    const row = document.createElement('div');
    row.className = 'ctx-row';
    const dot = document.createElement('span');
    dot.className = 'dot';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = s.source;
    name.title = s.source;
    const pct = document.createElement('span');
    pct.className = 'pct';
    pct.textContent = s.chunks + ' 段';
    const del = document.createElement('button');
    del.className = 'msg-act';
    del.textContent = '删除';
    del.style.marginLeft = '6px';
    del.onclick = () => kbRemoveSource(s.source);
    row.append(dot, name, pct, del);
    pane.appendChild(row);
  }
}

async function kbPickIndex() {
  const res = await api().knowledge_pick_index();
  kbNote = res.ok ? (res.summary || '已索引') : (res.error || '已取消');
  loadKnowledge();
}

async function kbClear() {
  const ok = await dialogConfirm('清空知识库', '确定清空所有已索引片段？索引文件将被删除，此操作不可恢复。');
  if (!ok) return;
  const res = await api().knowledge_clear();
  kbNote = res.ok ? (res.summary || '已清空知识库') : (res.error || '清空失败');
  loadKnowledge();
}

async function kbRemoveSource(name) {
  const ok = await dialogConfirm('删除来源',
    '删除来源「' + name + '」的全部片段？其余来源不受影响。');
  if (!ok) return;
  const res = await api().knowledge_remove_source(name);
  kbNote = res.ok ? res.summary : (res.error || res.summary || '删除失败');
  loadKnowledge();
}

/* ---------- 会话搜索（当前会话 / 全部历史，命中跳转） ---------- */
async function searchFlow() {
  const q = await dialogPrompt('搜索会话消息（在全部历史中查找，跳到当前会话的命中处）', '');
  if (q === null || !q.trim()) return;
  const keyword = q.trim();
  // 先给一个范围选择：全部历史 / 仅当前会话
  const scope = await dialogChoose('搜索范围', [
    { value: 'all', label: '🗂 全部会话历史', sub: keyword },
    { value: 'current', label: '💬 仅当前会话', sub: keyword },
  ]);
  if (scope === null) return;
  const res = await api().search_sessions(keyword, scope);
  if (!res.ok) { toast(res.error || '搜索失败', true); return; }
  const hits = res.hits || [];
  if (!hits.length) { toast('没有找到包含「' + keyword + '」的消息'); return; }
  const options = hits.map(h => ({
    value: h,
    label: (h.role === 'user' ? '👤 ' : '🤖 ') + h.session_name,
    sub: h.snippet,
  }));
  options.push({ value: '__cancel__', label: '取消', sub: '' });
  const pick = await dialogChoose(
    '找到 ' + hits.length + ' 条' + (res.truncated ? '（仅显示前 60 条，可换更精确的关键词）' : ''),
    options);
  if (!pick || pick === '__cancel__') return;
  if (pick.session !== currentSession) await selectSession(pick.session);
  const el = document.querySelector('#thread .msg[data-hi="' + pick.index + '"]');
  if (el) {
    el.scrollIntoView({ block: 'center' });
    el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
  }
}

/* ---------- 导出全部会话 zip ---------- */
async function exportAll() {
  const hint = $('statusHint');
  hint.textContent = '正在打包全部会话…';
  const res = await api().export_all_sessions();
  if (res.ok) {
    hint.textContent = '已导出: ' + res.path;
    setTimeout(() => { hint.textContent = ''; }, 8000);
  } else {
    hint.textContent = '';
    toast('导出失败: ' + (res.error || '未知错误'), true);
  }
}

/* ---------- 用量图表（近 30 天，按天聚合的 SVG 折线） ---------- */
async function loadSubagents() {
  const pane = $('subPane');
  if (!pane) return;
  pane.innerHTML = '<div class="hint">加载中…</div>';
  const r = await api().subagents(50);
  pane.innerHTML = '';
  const items = (r && r.items) || [];
  if (!items.length) {
    pane.innerHTML = '<div class="hint">还没有子 agent 调用记录。<br>' +
      '让主 agent「派个子 agent 去做某个独立子任务」即可，这里会列出每次调用的' +
      '任务、耗时、执行步骤与结论。</div>';
    return;
  }
  for (const it of items) {
    const row = document.createElement('div');
    row.className = 'sub-row';
    const when = it.ts ? new Date(it.ts * 1000).toLocaleString('zh-CN') : '';
    const secs = it.elapsed_ms ? (it.elapsed_ms / 1000).toFixed(1) + 's' : '';
    const head = document.createElement('div');
    head.className = 'sub-head';
    head.innerHTML = '<span class="sub-badge ' + (it.ok ? 'ok' : 'bad') + '">' +
      (it.ok ? '✓' : '✕') + '</span>' +
      '<span class="sub-task">' + esc((it.task || '（无任务描述）').slice(0, 80)) + '</span>' +
      '<span class="sub-time">' + esc(secs) + '</span>';
    head.title = it.task || '';
    head.onclick = () => row.classList.toggle('open');
    row.appendChild(head);
    if (it.id) {   // 旧版记录没有 id，无法按行定位删除，只渲染整表清空
      const rm = document.createElement('button');
      rm.className = 'mini-btn sub-rm';
      rm.textContent = '×';
      rm.title = '移除这条记录';
      rm.onclick = (e) => { e.stopPropagation(); removeSubagent(it.id); };
      head.appendChild(rm);
    }
    const body = document.createElement('div');
    body.className = 'sub-body';
    const trace = (it.trace || []);
    const steps = (it.steps || []);
    let traceHtml = '';
    if (trace.length) {
      traceHtml = '<div class="sub-steps">' + trace.slice(0, 25).map(t => {
        const res = (t.result || '').split('\n')[0].slice(0, 70);
        const bad = (t.result || '').startsWith('错误');
        return '<div class="sub-step' + (bad ? ' bad' : '') + '">🔧 ' + esc(t.name || '') +
          (res ? ' <span class="sub-res">' + esc(res) + '</span>' : '') + '</div>';
      }).join('') + '</div>';
    } else if (steps.length) {
      traceHtml = '<div class="sub-steps">🔧 ' + esc(steps.join(' · ')) + '</div>';
    }
    body.innerHTML = traceHtml +
      (it.error ? '<div class="sub-err">' + esc(it.error) + '</div>' : '') +
      '<div class="sub-out">' + esc((it.output || '（无结论）')) + '</div>' +
      '<div class="sub-meta">' + esc(when) + ' · ' + esc(it.sub_session || '') + '</div>';
    row.appendChild(body);
    pane.appendChild(row);
  }
}

async function removeSubagent(id) {
  const r = await api().remove_subagents([id]);
  if (!r || !r.ok) {
    toast('移除失败: ' + ((r && r.error) || '未知错误'), true);
    return;
  }
  loadSubagents();
}

async function clearSubagents() {
  const ok = await dialogConfirm('清空子 agent 记录',
    '确定清空全部子 agent 调用记录？此操作不可恢复。');
  if (!ok) return;
  const r = await api().remove_subagents(null);
  if (!r || !r.ok) {
    toast('清空失败: ' + ((r && r.error) || '未知错误'), true);
    return;
  }
  loadSubagents();
}

async function loadUsageChart() {
  const pane = $('usagePane');
  pane.innerHTML = '<div class="hint">加载中…</div>';
  const res = await api().usage_daily(30);
  if (!res.ok) {
    pane.innerHTML = '<div class="hint">' + esc(res.error || '加载失败') + '</div>';
    return;
  }
  renderUsageChart(pane, res.days || []);
}

function renderUsageChart(pane, pts) {
  const W = 560, H = 190, P = 38;
  const hasCached = pts.some(p => (p.cached || 0) > 0);
  const maxTok = Math.max(1, ...pts.map(p => p.total));
  const stepX = pts.length > 1 ? (W - 2 * P) / (pts.length - 1) : 0;
  const xy = (i, v) => [P + i * stepX, H - P - (v / maxTok) * (H - 2 * P)];
  const path = pts.map((p, i) => {
    const [x, y] = xy(i, p.total);
    return (i ? 'L' : 'M') + x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  const lastX = (P + (pts.length - 1) * stepX).toFixed(1);
  const area = path + ' L' + lastX + ',' + (H - P) + ' ' + P + ',' + (H - P) + ' Z';
  // 缓存命中折线（虚线，仅当数据里有 cached_tokens 时绘制）
  const cachedPath = hasCached
    ? pts.map((p, i) => {
        const [x, y] = xy(i, p.cached || 0);
        return (i ? 'L' : 'M') + x.toFixed(1) + ',' + y.toFixed(1);
      }).join(' ')
    : '';
  let grid = '';
  for (let g = 0; g <= 3; g++) {
    const v = maxTok * g / 3;
    const y = (H - P - (v / maxTok) * (H - 2 * P)).toFixed(1);
    grid += '<line x1="' + P + '" y1="' + y + '" x2="' + (W - P) + '" y2="' + y +
      '" stroke="var(--line-soft)" stroke-width="1"/>' +
      '<text x="' + (P - 5) + '" y="' + (Number(y) + 3) + '" text-anchor="end" font-size="9" fill="var(--faint)">' +
      fmtTokens(Math.round(v)) + '</text>';
  }
  let dots = '';
  pts.forEach((p, i) => {
    const [x, y] = xy(i, p.total);
    dots += '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="2.5" fill="var(--accent)">' +
      '<title>' + p.day + '｜共 ' + p.total + ' tokens（入 ' + p.prompt + ' / 出 ' + p.completion +
      '，' + p.calls + ' 轮）</title></circle>';
    if (i % 5 === 0 || i === pts.length - 1) {
      dots += '<text x="' + x.toFixed(1) + '" y="' + (H - P + 13) +
        '" text-anchor="middle" font-size="9" fill="var(--faint)">' + p.day.slice(5) + '</text>';
    }
  });
  const total = pts.reduce((a, p) => a + p.total, 0);
  const cachedLine = hasCached
    ? '<path d="' + cachedPath + '" fill="none" stroke="var(--dim)" stroke-width="1.5" stroke-dasharray="4 3"/>'
    : '';
  const cachedLegend = hasCached
    ? '<span><i style="background:var(--dim)"></i>其中缓存命中</span>'
    : '';
  pane.innerHTML =
    '<div class="chart-legend"><span><i></i>每日 tokens</span>' +
    cachedLegend +
    '<span>近 30 天累计 ' + fmtTokens(total) + '</span>' +
    '<span style="margin-left:auto">峰值 ' + fmtTokens(maxTok) + '</span></div>' +
    '<div class="chart-box"><svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMid meet">' +
    grid + '<path d="' + area + '" fill="var(--accent-soft)" stroke="none"/>' +
    '<path d="' + path + '" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/>' +
    cachedLine + dots + '</svg></div>' +
    '<div class="hint">数据来自 profile 的 usage.jsonl（每轮对话记录一条），悬停圆点看当天明细。</div>';
}

/* ---------- 完成提示音 / 窗口通知 ---------- */
function playBeep() {
  try {
    const ctx = playBeep._ctx ||
      (playBeep._ctx = new (window.AudioContext || window.webkitAudioContext)());
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.frequency.value = 830;
    gain.gain.setValueAtTime(0.06, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.25);
    osc.start(); osc.stop(ctx.currentTime + 0.26);
  } catch (e) { /* 音频不可用就静默 */ }
}

function notifyDone() {
  if (uiPrefs.notify_sound) playBeep();
  if (uiPrefs.notify_desktop && document.hidden &&
      window.Notification && Notification.permission === 'granted') {
    try { new Notification('卅 harness', { body: '回复已完成，点回窗口查看。' }); } catch (e) {}
  }
}

async function toggleNotify(key) {
  uiPrefs[key] = !uiPrefs[key];
  if (key === 'notify_desktop' && uiPrefs[key] && window.Notification &&
      Notification.permission === 'default') {
    try { await Notification.requestPermission(); } catch (e) { /* 忽略 */ }
  }
  saveUiPrefs();
}

/* ---------- 自定义强调色 / 字体 ---------- */
function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec((hex || '').trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}
function shiftColor(rgb, amt) {
  const c = v => Math.max(0, Math.min(255, v + amt));
  return '#' + [c(rgb.r), c(rgb.g), c(rgb.b)]
    .map(v => v.toString(16).padStart(2, '0')).join('');
}
function applyAccent(hex) {
  const root = document.documentElement;
  const rgb = hexToRgb(hex);
  if (!rgb) {  // 空 / 非法 → 恢复主题默认
    ['--accent', '--accent-2', '--accent-grad', '--accent-soft', '--ring', '--accent-glow']
      .forEach(v => root.style.removeProperty(v));
    return;
  }
  const light = shiftColor(rgb, 46);
  root.style.setProperty('--accent', hex.startsWith('#') ? hex : '#' + hex);
  root.style.setProperty('--accent-2', light);
  root.style.setProperty('--accent-grad',
    'linear-gradient(135deg, #' + hex.replace('#', '') + ' 0%, ' + light + ' 100%)');
  root.style.setProperty('--accent-soft', 'rgba(' + rgb.r + ',' + rgb.g + ',' + rgb.b + ',.15)');
  root.style.setProperty('--ring', '0 0 0 3px rgba(' + rgb.r + ',' + rgb.g + ',' + rgb.b + ',.22)');
  root.style.setProperty('--accent-glow', '0 4px 16px rgba(' + rgb.r + ',' + rgb.g + ',' + rgb.b + ',.35)');
}
function applyFont(f) {
  document.body.style.fontFamily = f
    ? "'" + f.replace(/'/g, '') + "', 'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif"
    : '';
}
/* ---------- 设置页：外观（主题 / 强调色 / 字体） ---------- */
const ACCENT_PRESETS = [
  ['', '默认（靛蓝）', '#5b7cfa'],
  ['#2f9bff', '天蓝', '#2f9bff'],
  ['#22b573', '翠绿', '#22b573'],
  ['#f08c3a', '暖橙', '#f08c3a'],
  ['#e5588a', '玫红', '#e5588a'],
  ['#8b5cf6', '紫', '#8b5cf6'],
];

async function pickTheme(mode) {
  applyTheme(mode);
  await api().set_theme(mode);
  renderAppearance();
}

function applyCustomAccent() {
  const hex = $('accentInput').value.trim();
  if (hex && !hexToRgb(hex)) {
    $('appearanceNote').className = 'note-err';
    $('appearanceNote').textContent = '颜色格式不对，需要 #RRGGBB';
    return;
  }
  uiPrefs.accent = hex;
  applyAccent(hex);
  saveUiPrefs();
  $('appearanceNote').className = 'note-ok';
  $('appearanceNote').textContent = hex ? '已应用强调色 ' + hex : '已恢复默认强调色';
  renderAppearance();
}

function applyCustomFont() {
  uiPrefs.font = $('fontInput').value.trim();
  applyFont(uiPrefs.font);
  saveUiPrefs();
  $('appearanceNote').className = 'note-ok';
  $('appearanceNote').textContent = uiPrefs.font ? '已应用字体 ' + uiPrefs.font : '已恢复默认字体';
  renderAppearance();
}

function renderAppearance() {
  document.querySelectorAll('#themeSeg button').forEach(b =>
    b.classList.toggle('sel', b.textContent.includes(THEME_TEXT[theme])));
  const box = $('swatches');
  box.innerHTML = '';
  for (const [hex, label, demo] of ACCENT_PRESETS) {
    const sw = document.createElement('button');
    sw.type = 'button';
    sw.className = 'swatch' + ((uiPrefs.accent || '') === hex ? ' sel' : '');
    sw.style.background = hex || demo;
    sw.title = label;
    sw.onclick = () => {
      uiPrefs.accent = hex;
      applyAccent(hex);
      saveUiPrefs();
      renderAppearance();
    };
    box.appendChild(sw);
  }
  $('accentInput').value = uiPrefs.accent || '';
  $('fontInput').value = uiPrefs.font || '';
  $('appearanceNote').textContent = '';
}

/* ---------- 设置页：通用（界面开关 / 数据） ---------- */
const GENERAL_TOGGLES = [
  { label: '工作区选择条', get: () => !wsBarDismissed, act: () => toggleWsBar() },
  { label: '右侧面板', get: () => !document.body.classList.contains('panel-hidden'),
    act: () => togglePanel() },
  { label: '左侧栏', get: () => !sideHidden, act: () => toggleSide() },
  { label: '回复完成提示音', get: () => !!uiPrefs.notify_sound,
    act: () => toggleNotify('notify_sound') },
  { label: '窗口通知（最小化时）', get: () => !!uiPrefs.notify_desktop,
    act: () => toggleNotify('notify_desktop') },
  { label: '显示已完成的会话', get: () => !!uiPrefs.show_done,
    act: () => { uiPrefs.show_done = !uiPrefs.show_done; saveUiPrefs(); refreshSidebar(); } },
];

function renderGeneral() {
  const toggles = $('generalToggles');
  toggles.innerHTML = '';
  for (const t of GENERAL_TOGGLES) {
    const row = document.createElement('div');
    row.className = 'set-row';
    const main = document.createElement('div');
    main.className = 'set-main';
    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = t.label;
    main.appendChild(name);
    const btn = document.createElement('button');
    btn.className = 'msg-act';
    btn.textContent = t.get() ? '已开启' : '已关闭';
    btn.onclick = async () => { await t.act(); renderGeneral(); };
    row.append(main, btn);
    toggles.appendChild(row);
  }
  const cw = $('confirmWriteBtn');
  if (cw) cw.textContent = confirmWriteOn ? '已开启' : '已关闭';
  loadCheckpoints();
  $('generalNote').textContent = '';
}

/* 「写文件前显示 diff 确认」开关（服务端配置 permissions.confirm_write） */
let confirmWriteOn = false;
async function toggleConfirmWrite() {
  confirmWriteOn = !confirmWriteOn;
  await api().set_confirm_write(confirmWriteOn);
  const cw = $('confirmWriteBtn');
  if (cw) cw.textContent = confirmWriteOn ? '已开启' : '已关闭';
  toast(confirmWriteOn ? '写文件前会显示 diff 确认' : '已关闭写前确认（仍会自动留底）');
}

/* ---------- 设置页：用量统计 ---------- */
async function loadUsageTab() {
  const stats = $('usageStats');
  stats.innerHTML = '<div class="hint">加载中…</div>';
  const [u, d] = await Promise.all([api().usage(), api().usage_daily(30)]);
  if (!d.ok) {
    stats.innerHTML = '<div class="hint">' + esc(d.error || '加载失败') + '</div>';
    return;
  }
  const days = d.days || [];
  const sum = days.reduce((a, p) => ({
    total: a.total + p.total, calls: a.calls + p.calls,
    prompt: a.prompt + p.prompt, completion: a.completion + p.completion,
  }), { total: 0, calls: 0, prompt: 0, completion: 0 });
  const today = days.length ? days[days.length - 1] : { total: 0, calls: 0 };
  const life = u.usage || {};
  const lifeTotal = (life.prompt_tokens || 0) + (life.completion_tokens || 0);
  const card = (v, l) => '<div class="stat"><div class="v">' + v + '</div><div class="l">' + l + '</div></div>';
  stats.innerHTML =
    card(fmtTokens(today.total), '今日 tokens（' + today.calls + ' 轮）') +
    card(fmtTokens(sum.total), '近 30 天 tokens（' + sum.calls + ' 轮）') +
    card(fmtTokens(sum.prompt) + ' / ' + fmtTokens(sum.completion), '近 30 天 输入 / 输出') +
    card(fmtTokens(lifeTotal), '本进程累计 tokens');
  renderUsageChart($('usageChartHolder'), days);
}

/* ---------- 设置页入口的数据动作（先关弹窗再执行，避免遮挡跳转结果） ---------- */
function settingsSearch() {
  closeSettings();
  searchFlow();
}
function settingsExport(all) {
  closeSettings();
  if (all) exportAll();
  else exportCurrent();
}

/* 跳到审查面板配置远程仓库（若该标签页此前被隐藏，先找回） */
function gotoReview() {
  closeSettings();
  if (rpHidden.includes('review')) {
    rpHidden = rpHidden.filter(n => n !== 'review');
    saveUiPrefs();
    applyTabVisibility();
  }
  switchPanel('review');
  loadGitRemote();
  setTimeout(() => { const el = $('gitRemoteUrl'); if (el) el.focus(); }, 80);
}

/* ---------- 初始化工程（Bootstrap：目录 + git 基线 + TODO.md） ---------- */
async function bootstrapFlow() {
  const goal = await dialogPrompt('工程目标（一句话，用于 README 与 TODO.md；可留空）', '');
  if (goal === null) return;
  showLiveBlock(); setBusy(true);
  const res = await api().bootstrap_project(goal.trim());
  removeLiveBlock(); setBusy(false);
  if (!res.ok) {
    toast('Bootstrap 失败: ' + (res.error || '未知错误'), true);
    return;
  }
  const parts = (res.created || []).slice();
  toast('Bootstrap 完成：' + (parts.length ? parts.join('、') : '工作区已就绪') +
    (res.committed ? ' · 已提交基线' : ''));
  if (res.commit_error) add('基线提交未完成: ' + res.commit_error, 'bot error');
  if (res.todo_note) toast('TODO.md：' + res.todo_note);
  refreshSidebar();
  loadWsTree();
}

/* ---------- 快捷指令（常用提示词片段，一键插入） ---------- */
async function snipMenu() {
  const res = await api().snippets_list();
  const items = res.snippets || [];
  const ta = $('input');
  if (!items.length) {
    // 一条都没有：引导从当前输入新建
    const name = await dialogPrompt('新建快捷指令：名称', '');
    if (name === null || !name.trim()) return;
    const text = await dialogPrompt('提示词内容', ta.value.trim());
    if (text === null || !text.trim()) return;
    const r = await api().snippets_save(name.trim(), text.trim());
    if (!r.ok) add(r.error || '保存失败', 'bot error');
    return;
  }
  const options = items.map(s => ({
    value: 'use:' + s.name, label: s.name, sub: s.text.split('\n')[0].slice(0, 40),
  }));
  if (ta.value.trim()) options.push({ value: 'add', label: '＋ 把当前输入存为指令', sub: '' });
  options.push({ value: 'del', label: '🗑 删除指令…', sub: '' });
  const pick = await dialogChoose('快捷指令（选中后插入输入框）', options);
  if (pick === null) return;
  if (pick.startsWith('use:')) {
    const s = items.find(i => i.name === pick.slice(4));
    if (s) {
      ta.value = ta.value.trim() ? ta.value.trimEnd() + '\n' + s.text : s.text;
      ta.dispatchEvent(new Event('input'));
      ta.focus();
    }
  } else if (pick === 'add') {
    const name = await dialogPrompt('指令名称', '');
    if (name === null || !name.trim()) return;
    const r = await api().snippets_save(name.trim(), ta.value.trim());
    if (!r.ok) add(r.error || '保存失败', 'bot error');
  } else if (pick === 'del') {
    const del = await dialogChoose('删除哪个指令？',
      items.map(s => ({ value: s.name, label: s.name, sub: s.text.split('\n')[0].slice(0, 40) })));
    if (del !== null) await api().snippets_delete(del);
  }
}

/* ---------- 拖拽文件入窗（图片 → 附加；文本 → 插入输入框） ---------- */
const DROP_IMG_RE = /\.(png|jpe?g|gif|webp)$/i;
const DROP_TEXT_RE = /\.(md|txt|py|js|ts|json|yaml|yml|html|css|go|saho|csv|rst|toml|ini|sh|bat|c|cpp|h|hpp|java|rs|xml|sql)$/i;
let dragDepth = 0;

window.addEventListener('dragenter', (e) => {
  e.preventDefault();
  dragDepth++;
  document.body.classList.add('dragging');
});
window.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) document.body.classList.remove('dragging');
});
window.addEventListener('dragover', (e) => { e.preventDefault(); });
window.addEventListener('drop', async (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove('dragging');
  for (const f of (e.dataTransfer.files || [])) {
    if (DROP_IMG_RE.test(f.name)) {
      const p = f.path || '';  // WebView2 会带上磁盘路径；拿不到就只能提示
      if (!p) {
        toast('拿不到「' + f.name + '」的磁盘路径，请用 📎 选择或粘贴完整路径', true);
        continue;
      }
      const chk = await api().check_image(p);
      if (chk.ok) addImagePath(chk.path);
      else toast(chk.error, true);
    } else if (DROP_TEXT_RE.test(f.name) && f.size <= 200 * 1024) {
      const text = await f.text();
      const ta = $('input');
      const head = '【文件: ' + f.name + '】\n';
      const body = text.slice(0, 8000) + (text.length > 8000 ? '\n…（已截断）' : '');
      ta.value = ta.value.trim() ? ta.value.trimEnd() + '\n\n' + head + body : head + body;
      ta.dispatchEvent(new Event('input'));
      ta.focus();
    } else {
      toast('已跳过「' + f.name + '」——只支持图片或 200KB 内的文本文件', true);
    }
  }
});

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
    if (e.dir) {
      row.onclick = () => { wsPath = wsPath ? wsPath + '/' + e.name : e.name; loadWsTree(); };
    } else {
      row.title = '点击预览：' + e.name;
      row.onclick = () => previewWsFile(e.name);
    }
    frag.appendChild(row);
  }
  pane.appendChild(frag);
}
/* ---------- 文件预览（工作区面板内） ---------- */
const FP_KINDS = { image: '图片', markdown: 'Markdown', table: '表格数据',
  code: '文本/代码', html: 'HTML 页面', pdf: 'PDF', office: 'Office 文档',
  archive: '压缩包', audio: '音频', video: '视频', binary: '二进制', unknown: '未知' };
const FP_KW = {
  py: ['def', 'class', 'return', 'if', 'elif', 'else', 'for', 'while', 'import',
       'from', 'try', 'except', 'finally', 'with', 'as', 'lambda', 'yield', 'None',
       'True', 'False', 'and', 'or', 'not', 'in', 'is', 'raise', 'async', 'await'],
  js: ['const', 'let', 'var', 'function', 'return', 'if', 'else', 'for', 'while',
       'class', 'new', 'import', 'export', 'from', 'async', 'await', 'try', 'catch',
       'throw', 'typeof', 'this', 'null', 'undefined', 'true', 'false', 'switch',
       'case', 'default', 'break', 'continue', 'extends', 'super'],
  go: ['func', 'package', 'import', 'var', 'const', 'type', 'struct', 'interface',
       'return', 'if', 'else', 'for', 'range', 'switch', 'case', 'default', 'go',
       'defer', 'chan', 'map', 'nil', 'true', 'false'],
  rs: ['fn', 'let', 'mut', 'struct', 'enum', 'impl', 'trait', 'use', 'pub', 'mod',
       'match', 'if', 'else', 'for', 'while', 'loop', 'return', 'Some', 'None', 'Ok', 'Err'],
  sh: ['if', 'then', 'fi', 'else', 'elif', 'for', 'in', 'do', 'done', 'while',
       'case', 'esac', 'function', 'export', 'local', 'echo'],
  sql: ['select', 'from', 'where', 'join', 'left', 'inner', 'group', 'order', 'by',
        'insert', 'update', 'delete', 'create', 'table', 'as', 'and', 'or', 'limit'],
  saho: ['函数', '如果', '否则', '循环', '当', '返回', '变量', '定义', '输出', '读取', '引入', '真', '假'],
};
FP_KW.ts = (FP_KW.js || []).concat(['interface', 'type', 'enum', 'public', 'private']);
FP_KW.tsx = FP_KW.ts;
FP_KW.jsx = FP_KW.js;
FP_KW.mjs = FP_KW.js;
FP_KW.cjs = FP_KW.js;
FP_KW.java = ['public', 'private', 'class', 'static', 'void', 'final', 'new', 'return',
              'if', 'else', 'for', 'while', 'try', 'catch', 'import', 'package', 'extends'];
FP_KW.c = ['int', 'char', 'float', 'double', 'void', 'return', 'if', 'else', 'for',
           'while', 'struct', 'typedef', 'static', 'const', 'include', 'define'];
FP_KW.cpp = (FP_KW.c || []).concat(['class', 'public', 'private', 'template', 'namespace', 'new']);
FP_KW.cs = FP_KW.java;
FP_KW.php = ['function', 'class', 'public', 'private', 'echo', 'if', 'else', 'foreach',
             'return', 'new', 'namespace', 'use'];

// 轻量语法着色：先按正则切词再逐段转义，绝不在已生成的 HTML 上做替换
// （那样会把 class="c-com" 之类的名字也当关键字染色，导致结构错乱）
function hlCode(text, ext) {
  const kw = new Set(FP_KW[String(ext || '').replace('.', '')] || []);
  const re = /(\/\/[^\n]*|#[^\n]*|--[^\n]*|\/\*[\s\S]*?\*\/|<!--[\s\S]*?-->)|("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)|(\b\d[\w.]*\b)|([A-Za-z_一-龥][\w一-龥]*)/g;
  let out = '', last = 0, m;
  const push = (raw, cls) => {
    out += cls ? '<span class="' + cls + '">' + esc(raw) + '</span>' : esc(raw);
  };
  while ((m = re.exec(text)) !== null) {
    push(text.slice(last, m.index), '');
    if (m[1]) push(m[0], 'c-com');
    else if (m[2]) push(m[0], 'c-str');
    else if (m[3]) push(m[0], 'c-num');
    else push(m[0], kw.has(m[0]) ? 'c-kw' : '');
    last = m.index + m[0].length;
  }
  push(text.slice(last), '');
  return out;
}

function fpTable(text, sep) {
  // CSV/TSV 简易表格（最多 300 行、每行最多 40 列）
  const lines = text.split(/\r?\n/).filter(l => l.trim()).slice(0, 300);
  if (!lines.length) return '<div class="hint">（空文件）</div>';
  const rows = lines.map(l => l.split(sep).slice(0, 40));
  const head = rows[0];
  let h = '<div class="fp-table"><table><thead><tr>';
  for (const c of head) h += '<th>' + esc(c) + '</th>';
  h += '</tr></thead><tbody>';
  for (const r of rows.slice(1)) {
    h += '<tr>';
    for (const c of r) h += '<td>' + esc(c) + '</td>';
    h += '</tr>';
  }
  return h + '</tbody></table></div>';
}

async function previewWsFile(name) {
  const rel = wsPath ? wsPath + '/' + name : name;
  const box = $('filePreview');
  box.classList.add('open');
  box.innerHTML = '<div class="hint">预览加载中…</div>';
  const r = await api().file_preview(rel);
  if (!r.ok) {
    box.innerHTML = '<div class="hint">' + esc(r.error || '无法预览') + '</div>';
    return;
  }
  const size = (r.size || 0) >= 1024
    ? ((r.size / 1024).toFixed(1) + ' KB') : (r.size + ' B');
  const when = r.mtime ? new Date(r.mtime * 1000).toLocaleString('zh-CN') : '';
  const head = document.createElement('div');
  head.className = 'fp-head';
  const nm = document.createElement('span');
  nm.className = 'fp-name';
  nm.textContent = '📄 ' + r.name;
  const meta = document.createElement('span');
  meta.className = 'fp-meta';
  meta.textContent = (FP_KINDS[r.kind] || r.kind) + ' · ' + size +
    (when ? ' · ' + when : '') + (r.truncated ? ' · 已截断（>256KB）' : '') +
    (r.note ? ' · ' + r.note : '');
  const sp = document.createElement('span');
  sp.className = 'sp';
  const bOpen = document.createElement('button');
  bOpen.className = 'mini-btn';
  bOpen.textContent = '用系统打开';
  bOpen.onclick = () => openFileExternal(r.rel);
  const bClose = document.createElement('button');
  bClose.className = 'mini-btn';
  bClose.textContent = '✕';
  bClose.onclick = closeFilePreview;
  head.append(nm, meta, sp, bOpen, bClose);

  const body = document.createElement('div');
  if (r.kind === 'image') {
    const img = document.createElement('img');
    img.className = 'fp-img';
    img.src = r.data_url || '';
    img.alt = r.name;
    body.appendChild(img);
  } else if (r.kind === 'pdf' && r.data_url) {
    const fr = document.createElement('iframe');
    fr.className = 'fp-frame';
    fr.src = r.data_url;
    fr.title = r.name;
    body.appendChild(fr);
  } else if (r.kind === 'audio' && r.data_url) {
    const au = document.createElement('audio');
    au.className = 'fp-media';
    au.controls = true;
    au.src = r.data_url;
    body.appendChild(au);
  } else if (r.kind === 'video' && r.data_url) {
    const vd = document.createElement('video');
    vd.className = 'fp-media';
    vd.controls = true;
    vd.src = r.data_url;
    body.appendChild(vd);
  } else if (r.kind === 'html' && r.text !== undefined) {
    // HTML：默认沙箱渲染（禁脚本/同源），可切换看源码
    const wrap = document.createElement('div');
    wrap.className = 'fp-html-wrap';
    const view = document.createElement('div');
    view.className = 'fp-html-view';
    const fr = document.createElement('iframe');
    fr.className = 'fp-frame';
    fr.setAttribute('sandbox', '');
    fr.srcdoc = r.text;
    view.appendChild(fr);
    let asSource = false;
    const bToggle = document.createElement('button');
    bToggle.className = 'mini-btn';
    bToggle.textContent = '查看源码';
    bToggle.onclick = () => {
      asSource = !asSource;
      bToggle.textContent = asSource ? '渲染页面' : '查看源码';
      view.innerHTML = '';
      if (asSource) {
        const pre = document.createElement('pre');
        pre.className = 'fp-code';
        pre.innerHTML = hlCode(r.text || '', r.ext);
        view.appendChild(pre);
      } else {
        const f2 = document.createElement('iframe');
        f2.className = 'fp-frame';
        f2.setAttribute('sandbox', '');
        f2.srcdoc = r.text;
        view.appendChild(f2);
      }
    };
    const bar = document.createElement('div');
    bar.className = 'fp-html-bar';
    bar.appendChild(bToggle);
    wrap.append(bar, view);
    body.appendChild(wrap);
  } else if (r.kind === 'markdown') {
    body.innerHTML = md(r.text || '');
  } else if (r.kind === 'table') {
    body.innerHTML = fpTable(r.text || '', r.ext === '.tsv' ? '\t' : ',');
  } else if (r.text !== undefined && r.kind !== 'unknown') {
    // code / office 文本 / 压缩包清单 / 二进制摘要：统一按等宽文本呈现
    const pre = document.createElement('pre');
    pre.className = 'fp-code';
    pre.innerHTML = (r.kind === 'code' || r.kind === 'html')
      ? hlCode(r.text || '', r.ext) : esc(r.text || '');
    body.appendChild(pre);
  } else {
    const p = document.createElement('div');
    p.className = 'fp-info';
    const why = {
      office: '该 Office 格式（老格式或 >8MB）无法在面板内解析',
      archive: '该压缩格式（非 zip 或 >8MB）无法在面板内列出内容',
      audio: '音频超过内嵌上限',
      video: '视频超过内嵌上限',
      pdf: 'PDF 超过内嵌上限',
      binary: '二进制内容无法解析',
    }[r.kind] || '该类型暂不支持内嵌预览';
    p.textContent = why + ' —— 点「用系统打开」用本机默认程序查看。';
    body.appendChild(p);
  }
  box.innerHTML = '';
  box.append(head, body);
}

function closeFilePreview() {
  const box = $('filePreview');
  if (!box) return;
  box.classList.remove('open');
  box.innerHTML = '';
}

async function openFileExternal(rel) {
  const r = await api().open_external(rel);
  if (!r.ok) toast(r.error || '打开失败', true);
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

/* ---------- 浏览器（多标签页，各自独立 iframe） ---------- */
let brTabsArr = [];   // [{id, url}]
let brActive = 0;
let brSeq = 0;

function renderBrTabs() {
  const box = $('brTabs');
  box.innerHTML = '';
  brTabsArr.forEach((t, i) => {
    const el = document.createElement('div');
    el.className = 'br-tab' + (i === brActive ? ' active' : '');
    let host = t.url;
    try { host = t.url ? (new URL(t.url).host || t.url) : '新标签页'; } catch (err) { host = t.url; }
    el.innerHTML = '<span class="bt-name" title="' + esc(t.url || '新标签页') + '">' +
      esc(host) + '</span><span class="bt-x" title="关闭此标签页">✕</span>';
    el.querySelector('.bt-name').onclick = () => { brActive = i; renderBrTabs(); showBrFrame(); };
    el.querySelector('.bt-x').onclick = (e) => { e.stopPropagation(); brClose(i); };
    box.appendChild(el);
  });
  const add = document.createElement('button');
  add.type = 'button';
  add.className = 'br-add';
  add.textContent = '＋';
  add.title = '新标签页';
  add.onclick = () => {
    brTabsArr.push({ id: ++brSeq, url: '' });
    brActive = brTabsArr.length - 1;
    renderBrTabs(); showBrFrame();
  };
  box.appendChild(add);
}

function showBrFrame() {
  const frames = $('brFrames');
  frames.querySelectorAll('iframe').forEach(f => f.classList.remove('on'));
  const t = brTabsArr[brActive];
  $('brEmpty').style.display = (t && t.url) ? 'none' : 'flex';
  $('brUrl').value = (t && t.url) || '';
  if (!t) return;
  let f = frames.querySelector('iframe[data-bid="' + t.id + '"]');
  if (!f) {
    f = document.createElement('iframe');
    f.dataset.bid = String(t.id);
    f.src = 'about:blank';
    frames.appendChild(f);
  }
  f.classList.add('on');
}

function brClose(i) {
  const t = brTabsArr[i];
  if (t) {
    const f = $('brFrames').querySelector('iframe[data-bid="' + t.id + '"]');
    if (f) f.remove();
  }
  brTabsArr.splice(i, 1);
  if (!brTabsArr.length) brTabsArr.push({ id: ++brSeq, url: '' });
  if (brActive >= brTabsArr.length) brActive = brTabsArr.length - 1;
  renderBrTabs();
  showBrFrame();
}

function brGo() {
  let url = $('brUrl').value.trim();
  if (!url) return;
  if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
  $('brUrl').value = url;
  if (!brTabsArr[brActive]) brTabsArr[brActive] = { id: ++brSeq, url: '' };
  const t = brTabsArr[brActive];
  t.url = url;
  const frames = $('brFrames');
  let f = frames.querySelector('iframe[data-bid="' + t.id + '"]');
  if (!f) {
    f = document.createElement('iframe');
    f.dataset.bid = String(t.id);
    frames.appendChild(f);
  }
  f.src = url;
  $('brEmpty').style.display = 'none';
  renderBrTabs();
}
function brExternal() {
  const url = $('brUrl').value.trim();
  if (url) api().open_url(/^https?:\/\//i.test(url) ? url : 'https://' + url);
}
renderBrTabs();
showBrFrame();

async function loadReview() {
  loadGitRemote();
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
  const model = $('auxModelSel') ? $('auxModelSel').value : '';
  const res = await api().chat(text, AUX_SESSION, null, model);
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
      rd.innerHTML = '<div class="r-head"><span class="r-title">思考过程</span>' +
        '<span class="r-caret-end">›</span></div><div class="r-body"></div>';
      rd.querySelector('.r-body').textContent = reasoning;
      rd.querySelector('.r-head').onclick = () => rd.classList.toggle('open');
      div.appendChild(rd);
    }
    const body = document.createElement('div');
    body.className = 'md-body';
    body.innerHTML = md(text);
    div.appendChild(body);
  }
  $('auxThread').appendChild(div);
}
async function auxInit() {
  // 模型下拉：辅助对话可单独选模型（不切全局当前模型）
  try {
    const s = await api().get_models();
    const sel = $('auxModelSel');
    sel.innerHTML = '';
    for (const m of (s.models || [])) {
      if (!m.name) continue;
      const o = document.createElement('option');
      o.value = m.name;
      o.textContent = m.name + (m.name === s.default_model ? '（默认）' : '');
      sel.appendChild(o);
    }
    if (s.default_model) sel.value = s.default_model;
  } catch (e) { /* 模型列表取不到就不显示可选模型 */ }
  const res = await api().session_history(AUX_SESSION);
  // 思考过程随消息落盘，回放时同样重建折叠块
  for (const m of (res.history || [])) {
    auxAdd(m.content, m.role === 'user' ? 'user' : 'bot', m.reasoning || '');
  }
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
$('kbqInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); kbQuery(); }
});
$('brUrl').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); brGo(); }
});

/* ---------- 启动 ---------- */
window.addEventListener('pywebviewready', async () => {
  // 整条启动链互相隔离：任何一步抛错都只影响那一步，并在聊天区显示
  // 具体错误——绝不让整个聊天区「莫名空白」且无从排查
  const step = async (name, fn) => {
    try { await fn(); }
    catch (err) {
      try {
        add('启动步骤「' + name + '」失败: ' + (err && err.message || err), 'bot error');
      } catch (e2) { /* 连 add 都失败时至少留在 console */ }
      console.error('[boot]', name, err);
    }
  };
  // localStorage 快速恢复外观（不等后端，避免闪一下默认色）
  const savedAccent = lsGet('sh_accent');
  if (savedAccent) { uiPrefs.accent = savedAccent; applyAccent(savedAccent); }
  const savedFont = lsGet('sh_font');
  if (savedFont) { uiPrefs.font = savedFont; applyFont(savedFont); }
  await step('状态加载', async () => {
    await refreshStatus();
    await refreshSidebar();
  });
  await step('界面偏好恢复', async () => {
    const p = await api().get_ui_prefs();
    if (p && p.ok) applyUiPrefs(p.prefs);
  });
  await step('会话历史加载', async () => {
    const s = await api().session_history(currentSession);
    (s.history || []).filter(m => (m.content || '').trim()).forEach((m, i) =>
      add(m.content, m.role === 'user' ? 'user' : 'bot', null, null, i));
    showHeroIfEmpty();
  });
  await step('辅助对话初始化', async () => {
    await auxInit();
  });
  updateSendState();
  $('input').focus();
});
</script>    <div id="approveMask" class="ap-mask">
      <div class="ap-card">
        <div class="ap-head"><span id="apTitle">确认执行</span><span class="sp"></span>
          <span id="apCount" class="ap-count"></span></div>
        <div id="apDetail" class="ap-detail"></div>
        <div class="ap-foot">
          <button class="ghost" onclick="approveReply('deny')">拒绝</button>
          <span class="flex1"></span>
          <button class="ghost" id="apAlways" onclick="approveReply('allow_always')">一直允许</button>
          <button class="primary" onclick="approveReply('allow_once')">允许一次</button>
        </div>
      </div>
    </div>
</body></html>"""


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
