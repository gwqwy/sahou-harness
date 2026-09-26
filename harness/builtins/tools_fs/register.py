"""文件工具插件：工作区路径监狱。

缺陷审计修复：
- H-01：写入门改为白名单判定（``permission_mode`` 返回 "allow" 才放行），
  ``fs`` 默认值与 ``shell`` 一致为 ``ask``。
- N-01：``list_files`` 的 glob 模式此前直接交给 ``Path.glob``，而 ``..`` 会被
  glob 展开 —— 模型传 ``"../*"`` 即可绕过 ``_safe`` 读出工作区外文件。
  现在先拒绝模式本身，再对每个命中项做 ``_safe`` 兜底。
"""

from __future__ import annotations

from pathlib import Path

# 注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
from harness.config import permission_mode

IGNORED_PARTS = {"__pycache__", ".git", "node_modules", ".venv"}


def register(ctx) -> None:
    host = ctx.host

    def _workspace() -> Path:
        # 调用时动态读取：支持桌面端热切换工作区
        ws = getattr(host, "workspace", None)
        return Path(ws).resolve() if ws else Path.cwd().resolve()

    def _safe(path: str, must_exist: bool = False) -> Path:
        workspace = _workspace()
        candidate = Path(path)
        resolved = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
        if resolved != workspace and workspace not in resolved.parents:
            raise ValueError(f"路径越出工作区: {path}（工作区: {workspace}）")
        if must_exist and not resolved.is_file():
            raise FileNotFoundError(f"文件不存在: {path}")
        return resolved

    def _guard_pattern(pattern: str) -> None:
        """glob 模式自身不得逃逸：拒绝绝对路径与任何 ``..`` 分量。

        ``Path.glob`` 会把 ``..`` 当作可展开分量，因此必须在交给它之前拦下。
        """
        raw = str(pattern or "").strip()
        if not raw:
            raise ValueError("glob 模式不能为空")
        probe = Path(raw)
        if probe.is_absolute() or raw.startswith(("/", "\\")):
            raise ValueError(f"glob 模式不允许绝对路径: {pattern}")
        if ".." in probe.parts or ".." in raw.replace("\\", "/").split("/"):
            raise ValueError(f"glob 模式不允许包含 '..': {pattern}")

    def read_file(path: str) -> str:
        """读取工作区文件（带行号，超长截断）。

        Args:
            path: 工作区相对路径
        """
        resolved = _safe(path, must_exist=True)
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        out = "\n".join(f"{i + 1:>4}: {line}" for i, line in enumerate(lines))
        return out[:8000] or "（空文件）"

    def write_file(path: str, content: str) -> str:
        """写入工作区文件（新建或覆盖，自动创建父目录）。

        Args:
            path: 工作区相对路径
            content: 完整文件内容
        """
        # 文件写入权限门：permissions.fs = ask / allow / deny（读不受限）
        mode = permission_mode(host.profile.load_config(), "fs")
        if mode == "deny":
            return "错误：文件写入被权限配置拒绝（permissions.fs=deny）"
        if mode != "allow":
            confirm = getattr(host, "confirm", None)
            if confirm is None or not confirm(f"写入文件: {path}"):
                return "错误：写入未获人工确认，已拒绝"
        resolved = _safe(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"已写入 {path}（{len(content.splitlines())} 行）"

    def list_files(pattern: str = "**/*") -> str:
        """按 glob 列出工作区文件（模式与命中项都被限制在工作区内）。

        Args:
            pattern: glob 模式，如 **/*.py
        """
        workspace = _workspace()
        _guard_pattern(pattern)
        matches = []
        for path in sorted(workspace.glob(pattern)):
            try:
                safe = _safe(str(path))          # 兜底：命中项必须仍在工作区内
            except ValueError:
                continue
            if safe.is_file() and not any(part in IGNORED_PARTS for part in safe.parts):
                matches.append(safe.relative_to(workspace).as_posix())
        return "\n".join(matches) or "（无匹配文件）"

    ctx.tools.register(read_file)
    ctx.tools.register(write_file)
    ctx.tools.register(list_files)
