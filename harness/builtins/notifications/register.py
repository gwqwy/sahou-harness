"""通知插件：把消息 POST 到用户配置的 webhook（功能4）。

配置（config.json 的 ``notifications`` 段）::

    "notifications": {
        "url": "https://example.com/hook",   // 必填；缺省 → 插件 not-configured 缺席
        "on_error": true,                    // 可选：订阅宿主 error 事件推送告警（默认 false）
        "timeout": 10                        // 可选：POST 超时秒数（默认 10）
    }

- ``notify(message, title)`` 工具：POST ``{"title", "message", "source", "ts"}``。
- ``on_error=true`` 时订阅宿主事件总线的 ``error`` 事件（插件激活失败等），
  把失败摘要推到同一 webhook —— 推送失败只记日志，绝不反过来炸宿主。

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
httpx 是 openai 的既有依赖，不新增安装负担。
"""

from __future__ import annotations

import time

# POST 的 JSON 载荷上限：webhook 不是文件传输通道，超长消息先截断
MAX_MESSAGE_CHARS = 4000


def _clip(text, limit: int = MAX_MESSAGE_CHARS) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


def register(ctx) -> None:
    host = ctx.host
    section = host.profile.load_config().get("notifications") or {}
    url = str(section.get("url") or "").strip()
    if not url:
        # 缺席约定：没配 url 就不注册任何东西，status 里能看出原因
        ctx.skipped.append("not-configured: config.json 缺少 notifications.url")
        return

    raw_timeout = section.get("timeout")
    timeout = 10
    if raw_timeout is not None:
        try:
            timeout = max(1, min(60, int(raw_timeout)))
        except (TypeError, ValueError):
            timeout = 10

    def _post(payload: dict) -> str:
        import httpx

        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload)
        except httpx.TimeoutException:
            return f"错误：webhook {timeout} 秒内无响应（{url}）"
        except httpx.HTTPError as exc:
            return f"错误：webhook 请求失败（{type(exc).__name__}: {exc}）"
        if 200 <= resp.status_code < 300:
            return f"已通知（HTTP {resp.status_code}）"
        return f"错误：webhook 返回 HTTP {resp.status_code}：{_clip(resp.text, 300)}"

    def notify(message: str, title: str = "") -> str:
        """发送一条通知到配置的 webhook（POST JSON）。

        Args:
            message: 通知正文
            title: 可选标题
        """
        text = str(message or "").strip()
        if not text:
            return "错误：message 不能为空"
        return _post({
            "title": _clip(title or "sahou-harness 通知", 200),
            "message": _clip(text),
            "source": "sahou-harness",
            "ts": time.time(),
        })

    ctx.tools.register(notify)
    ctx.provide("notify", notify)

    if section.get("on_error"):
        def _on_host_error(name: str = "", error: str = "") -> None:
            try:
                _post({
                    "title": "sahou-harness 插件错误",
                    "message": f"插件 {name} 出错：{error}",
                    "source": "sahou-harness",
                    "ts": time.time(),
                })
            except Exception:  # noqa: BLE001 —— 告警失败不能反过来炸宿主
                pass

        # ctx.on 的 disposer 自动入账本，插件卸载时一并摘除（H-04 的教训）
        ctx.on("error", _on_host_error)

    # 校验一次 URL 形态，给出早期反馈（真正的连通性由首次 notify 检验）
    if not url.startswith(("http://", "https://")):
        ctx.skipped.append(f"warn: notifications.url 不是 http(s) 地址（{url}），notify 会失败")
