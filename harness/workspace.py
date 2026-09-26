"""工作区路径监狱与文件遍历 —— 所有文件类插件共用的唯一实现。

为什么单独抽出来：「越界判定」分散在各插件里是安全上的坏味道 —— 改了一处漏一处
就会留下绕过口（N-01 就是 ``list_files`` 的 glob 模式能展开 ``..`` 而绕过 ``_safe``）。
`tools_fs` 与 `tools_search` 现在都只调用这里，判定逻辑只有一份。

插件由 loader 以合成模块名加载（无包上下文），因此只能用绝对导入
（``from harness.workspace import ...``）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

# 遍历时一律跳过的目录名：版本/构建/虚拟环境产物，既无信息量又会淹没上下文
IGNORED_PARTS = {
    "__pycache__", ".git", "node_modules", ".venv", ".eggs",
    "build", "build_pyi", "dist", ".mypy_cache", ".ruff_cache", ".pytest_cache",
}

# 搜索时单文件读取上限：超过即跳过，避免把大二进制/大日志喂进上下文
MAX_FILE_BYTES = 2 * 1024 * 1024


def workspace_root(host) -> Path:
    """当前工作区根目录。

    每次都动态读取 ``host.workspace`` —— 桌面端支持热切换工作区，
    缓存下来会指向旧目录。
    """
    ws = getattr(host, "workspace", None)
    return Path(ws).resolve() if ws else Path.cwd().resolve()


def safe_path(host, path: str, must_exist: bool = False) -> Path:
    """把用户/模型给的路径解析成工作区内的绝对路径，越界即抛 ValueError。"""
    workspace = workspace_root(host)
    candidate = Path(str(path))
    resolved = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError(f"路径越出工作区: {path}（工作区: {workspace}）")
    if must_exist and not resolved.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    return resolved


def guard_pattern(pattern: str) -> str:
    """glob 模式自身不得逃逸：拒绝绝对路径与任何 ``..`` 分量。

    ``Path.glob`` 会把 ``..`` 当可展开分量，所以必须在交给它之前拦下。
    """
    raw = str(pattern or "").strip()
    if not raw:
        raise ValueError("glob 模式不能为空")
    probe = Path(raw)
    if probe.is_absolute() or raw.startswith(("/", "\\")):
        raise ValueError(f"glob 模式不允许绝对路径: {pattern}")
    if ".." in probe.parts or ".." in raw.replace("\\", "/").split("/"):
        raise ValueError(f"glob 模式不允许包含 '..': {pattern}")
    return raw


def iter_files(host, pattern: str = "**/*", limit: int = 0) -> Iterator[Path]:
    """按 glob 遍历工作区内的文件。

    逐个命中项都再做一次 :func:`safe_path` 兜底（模式合规不代表命中项合规）；
    跳过 :data:`IGNORED_PARTS` 中的目录。``limit > 0`` 时最多产出 limit 个 ——
    给 ``list_files`` 这类会把结果直接灌进上下文的调用兜底。
    """
    workspace = workspace_root(host)
    guard_pattern(pattern)
    count = 0
    for path in sorted(workspace.glob(pattern)):
        try:
            safe = safe_path(host, str(path))
        except (ValueError, OSError):
            continue
        if not safe.is_file():
            continue
        if any(part in IGNORED_PARTS for part in safe.parts):
            continue
        yield safe
        count += 1
        if limit and count >= limit:
            return


def relative(host, path: Path) -> str:
    """转成相对工作区的 posix 路径（跨平台一致，且不泄露本机绝对路径）。"""
    return path.relative_to(workspace_root(host)).as_posix()


def read_text_file(path: Path, max_bytes: int = MAX_FILE_BYTES) -> str | None:
    """读文本文件；疑似二进制或超过 max_bytes 时返回 None（调用方跳过）。"""
    try:
        if path.stat().st_size > max_bytes:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:  # 含 NUL 字节基本可判定为二进制
        return None
    return raw.decode("utf-8", "replace")


# 多模态输入接受的图片扩展名（服务商按魔数识别，这里只是早期拒绝的闸门）
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def safe_image(host, source) -> str:
    """校验一个图片源（多模态输入），返回可直接传给 nanoagent 的形态。

    - http(s) URL / data: URL：原样放行（由服务商下载/解码）
    - 本地路径：必须落在工作区路径监狱内、文件存在、扩展名像图片
    - 其余一律抛 ValueError（给界面/模型看可读错误，而不是让服务商报 400）
    """
    text = str(source or "").strip()
    if not text:
        raise ValueError("图片路径不能为空")
    if text.startswith(("http://", "https://", "data:")):
        return text
    path = safe_path(host, text, must_exist=True)
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(
            f"不像图片文件: {path.name}（支持 {'/'.join(sorted(IMAGE_SUFFIXES))}）")
    return str(path)


def safe_images(host, sources) -> list[str] | None:
    """批量校验图片源；空输入返回 None（保持「无图」语义）。"""
    if sources is None:
        return None
    if not isinstance(sources, (list, tuple)):
        raise ValueError("images 必须是路径/URL 列表")
    result = [safe_image(host, item) for item in sources]
    return result or None
