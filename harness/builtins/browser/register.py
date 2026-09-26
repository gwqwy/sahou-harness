"""浏览器工具插件（功能6）：``fetch_url`` 抓网页并转纯文本。

模型此前对「工作区之外的世界」只有用户转述——给它一个受控的 HTTP GET：

- 仅 http/https（禁 file:/ftp: 等一切本地 scheme）
- 可选白名单 ``config.browser.allowed_domains``（如 ``["docs.python.org"]``）；
  配置了就只放行列表内域名（精确匹配或以其结尾），缺省不限制
- HTML → 文本：剔除 script/style/noscript 与全部标签，解码实体，
  折叠连续空行 —— 给模型看的是可读正文，不是标签汤
- 超时 / 非 2xx / 超长响应都返回**可读的中文错误**，而不是抛异常栈

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
httpx 是 openai 的既有依赖，不新增安装负担。
"""

from __future__ import annotations

from urllib.parse import urlsplit

DEFAULT_MAX_CHARS = 8000
MAX_CHARS_CAP = 50000
DEFAULT_TIMEOUT = 15
MAX_BODY_BYTES = 5 * 1024 * 1024


def _load_allowed_domains(host) -> list[str]:
    section = host.profile.load_config().get("browser") or {}
    raw = section.get("allowed_domains")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip().lower() for item in raw if str(item).strip()]


def _domain_allowed(domain: str, allowed: list[str]) -> bool:
    domain = domain.lower()
    return any(domain == item or domain.endswith("." + item) for item in allowed)


def html_to_text(html: str) -> str:
    """把 HTML 转成可读纯文本：去 script/style、去标签、解码实体、折叠空行。"""
    import html as html_module
    import re

    text = re.sub(r"(?is)<(script|style|noscript)\b[^>]*>.*?</\1>", " ", html)
    # 块级标签换成换行，保住基本的分行结构
    text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    # 相邻重复行折叠（模板造成的重复导航等）
    result: list[str] = []
    for line in lines:
        if not result or result[-1] != line:
            result.append(line)
    return "\n".join(result)


def register(ctx) -> None:
    host = ctx.host

    def fetch_url(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
        """抓取网页并返回纯文本（GET 请求，跟随重定向）。

        用于查阅在线文档、API 说明等公开网页。仅支持 http/https；
        配置了 browser.allowed_domains 白名单时只放行名单内的域名。

        Args:
            url: 完整的 http(s) 地址
            max_chars: 最多返回多少字符，默认 8000
        """
        import httpx

        text_url = str(url or "").strip()
        if not text_url:
            return "错误：url 不能为空"
        parts = urlsplit(text_url)
        if parts.scheme not in ("http", "https"):
            return f"错误：仅支持 http/https 地址，收到: {text_url}"
        if not parts.netloc:
            return f"错误：不是完整 URL（缺域名）: {text_url}"
        allowed = _load_allowed_domains(host)
        if allowed and not _domain_allowed(parts.hostname or "", allowed):
            return (f"错误：域名 {parts.hostname} 不在白名单内"
                    f"（browser.allowed_domains: {', '.join(allowed)}）")

        try:
            cap = max(100, min(int(max_chars), MAX_CHARS_CAP))
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_CHARS

        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True,
                              headers={"User-Agent": "sahou-harness/1.0 (fetch_url)"}) as client:
                resp = client.get(text_url)
        except httpx.TimeoutException:
            return f"错误：{DEFAULT_TIMEOUT} 秒内无响应（{text_url}）"
        except httpx.HTTPError as exc:
            return f"错误：请求失败（{type(exc).__name__}: {exc}）"
        if not (200 <= resp.status_code < 300):
            return f"错误：HTTP {resp.status_code}（{text_url}）"
        if len(resp.content) > MAX_BODY_BYTES:
            return f"错误：响应过大（{len(resp.content)} 字节，上限 {MAX_BODY_BYTES}）"

        content_type = resp.headers.get("content-type", "")
        if "html" in content_type or text_url.rstrip("/").endswith((".htm", ".html")):
            body = html_to_text(resp.text)
        else:
            body = resp.text
        if len(body) > cap:
            body = body[:cap] + f"\n\n[已截断：全文 {len(body)} 字符，可用更大的 max_chars 或直接访问原文]"
        return body

    ctx.tools.register(fetch_url)
