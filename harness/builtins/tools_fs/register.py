"""文件工具插件：工作区路径监狱内的读 / 写 / 改 / 列。

历史修复（保留说明，避免回归）：
- H-01：写入门改为白名单判定（``permission_mode`` 返回 "allow" 才放行），
  ``fs`` 默认值与 ``shell`` 一致为 ``ask``。
- N-01：``list_files`` 的 glob 模式此前直接交给 ``Path.glob``，而 ``..`` 会被
  glob 展开 —— 模型传 ``"../*"`` 即可绕过判定。现在模式与命中项都要过
  :mod:`harness.workspace` 的检查。

本轮补强：
- ``edit_file``：**局部编辑**。此前只能整文件重写，改一行要吐整个文件 ——
  token 贵、易错，还会覆盖掉文件里其它并发改动。
- ``read_file`` 支持 ``offset``/``limit`` 分页：此前固定截断 8000 字符，
  大文件的尾部永远读不到。
- ``list_files`` 输出加上限：此前无上限，实测本工作区可直接吐出 894 行
  （约 7 万字符）把上下文淹掉。

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
"""

from __future__ import annotations

from pathlib import Path

from harness.approvals import remember_fs_allow, request_approval
from harness.config import permission_mode
from harness.workspace import (
    IGNORED_PARTS,  # noqa: F401 —— 保留再导出，历史上从本模块引用过
    iter_files,
    relative,
    safe_path,
)

MAX_READ_CHARS = 8000        # 单次 read_file 回给模型的字符上限
DEFAULT_READ_LINES = 2000    # 单次 read_file 默认行数
DEFAULT_LIST_LIMIT = 200     # list_files 默认最多返回多少条


def _as_int(value, default: int) -> int:
    """模型可能按错误的 schema 传字符串；这里做一次宽容转换。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def register(ctx) -> None:
    host = ctx.host

    def _stats(old: str, new: str) -> str:
        """增删行统计（如 ``+5 -2``），供界面把改动量显示成小徽标。"""
        import difflib

        added = removed = 0
        try:
            for line in difflib.unified_diff(old.splitlines(), new.splitlines(),
                                             lineterm="", n=0):
                if line.startswith("+") and not line.startswith("+++"):
                    added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    removed += 1
        except Exception:  # noqa: BLE001
            return ""
        return f"，+{added} -{removed}"

    def _unified(old: str, new: str, path: str, limit: int = 200) -> str:
        """生成给用户看的 unified diff（超长截断，避免确认框被巨文件淹没）。"""
        import difflib

        try:
            lines = list(difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile="a/" + path, tofile="b/" + path, lineterm="", n=2))
        except Exception:  # noqa: BLE001 —— diff 只是展示，失败不影响主流程
            return ""
        if len(lines) > limit:
            lines = [*lines[:limit], f"…（差异过长，仅显示前 {limit} 行）"]
        return "\n".join(lines)

    def _gate(path: str, diff: str = "", existed: bool = True) -> str:
        """写入类操作的权限门：放行时返回空串，否则返回给模型看的错误文本。

        - ``permissions.fs`` = ask / allow / deny（读取不受限）
        - ``permissions.confirm_write``（默认关）：即使 fs=allow，
          也要求每次写入先看一眼 diff 再决定——给「怕改错」的人用
        """
        config = host.profile.load_config()
        mode = permission_mode(config, "fs")
        if mode == "deny":
            return "错误：文件写入被权限配置拒绝（permissions.fs=deny）"
        perms = config.get("permissions")
        confirm_every = bool(perms.get("confirm_write")) if isinstance(perms, dict) else False
        if mode == "allow" and not confirm_every:
            return ""
        decision = request_approval(host, {
            "kind": "write",
            "title": "写入文件",
            "path": path,
            "detail": "该文件已存在，将被覆盖" if existed else "新建文件",
            "diff": diff[:8000],
        })
        if decision == "deny":
            return "错误：写入未获人工确认，已拒绝"
        if decision == "allow_always":
            remember_fs_allow(host)
        return ""

    def _checkpoint(paths: list[Path], label: str) -> None:
        """写入前留底（失败不阻断写入：检查点是安全网，不是门）。"""
        try:
            from harness.checkpoints import snapshot

            snapshot(host, paths, label=label)
        except Exception:  # noqa: BLE001
            pass

    def read_file(path: str, offset: int = 1, limit: int = DEFAULT_READ_LINES) -> str:
        """读取工作区文件（带行号，可分段读取大文件）。

        Args:
            path: 工作区相对路径
            offset: 起始行号，从 1 开始
            limit: 最多读取多少行
        """
        resolved = safe_path(host, path, must_exist=True)
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        start = max(1, _as_int(offset, 1))
        count = max(1, _as_int(limit, DEFAULT_READ_LINES))
        if total == 0:
            return "（空文件）"
        if start > total:
            return f"（起始行 {start} 超出文件末尾：本文件共 {total} 行）"
        window = lines[start - 1: start - 1 + count]
        end = start - 1 + len(window)
        body = "\n".join(f"{i:>4}: {line}" for i, line in enumerate(window, start=start))
        notes = []
        if end < total:
            notes.append(f"本次只显示第 {start}-{end} 行（共 {total} 行），继续读用 offset={end + 1}")
        if len(body) > MAX_READ_CHARS:
            body = body[:MAX_READ_CHARS]
            notes.append(f"输出超过 {MAX_READ_CHARS} 字符已截断，请用 offset/limit 缩小范围")
        return body + ("\n\n[提示] " + "；".join(notes) if notes else "")

    def write_file(path: str, content: str) -> str:
        """写入工作区文件（新建或整文件覆盖，自动创建父目录）。

        若只是改动已有文件的局部，请优先用 edit_file —— 整文件覆盖容易带上未预期的改动。

        Args:
            path: 工作区相对路径
            content: 完整文件内容
        """
        resolved = safe_path(host, path)
        existed = resolved.is_file()
        old = resolved.read_text(encoding="utf-8", errors="replace") if existed else ""
        denial = _gate(path, _unified(old, content, path), existed=existed)
        if denial:
            return denial
        _checkpoint([resolved], f"write_file {path}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return (f"已写入 {path}（{len(content.splitlines())} 行"
                f"{_stats(old, content)}）")

    def edit_file(path: str, old: str, new: str, count: int = 1) -> str:
        """把工作区文件中的 old 精确替换为 new（局部编辑，无需重写整个文件）。

        old 必须与文件内容逐字符一致（含缩进）。默认要求**恰好命中 1 处**，
        命中数不符即报错不改动 —— 避免改到别处或漏改。

        Args:
            path: 工作区相对路径
            old: 待替换的原文（须逐字符一致，含缩进）
            new: 替换后的新文本
            count: 期望命中的处数，默认 1
        """
        if not old:
            return "错误：old 不能为空（想整文件重写请用 write_file）"
        if old == new:
            return "错误：old 与 new 完全相同，无需修改"
        resolved = safe_path(host, path, must_exist=True)
        text = resolved.read_text(encoding="utf-8", errors="replace")
        expected = max(1, _as_int(count, 1))
        found = text.count(old)
        if found == 0:
            return f"错误：在 {path} 中未找到待替换文本（注意缩进与换行需逐字符一致）"
        if found != expected:
            return (f"错误：{path} 中匹配到 {found} 处，与预期 count={expected} 不符，"
                    f"未做任何修改。请补充上下文使 old 唯一，或显式传 count={found}")
        lines_before = len(text.splitlines())
        updated = text.replace(old, new, expected)
        denial = _gate(path, _unified(text, updated, path), existed=True)
        if denial:
            return denial
        _checkpoint([resolved], f"edit_file {path}")
        resolved.write_text(updated, encoding="utf-8")
        lines_after = len(updated.splitlines())
        return (f"已修改 {path}（替换 {expected} 处，行数 {lines_before} → {lines_after}"
                f"{_stats(text, updated)}）")

    def list_files(pattern: str = "**/*", max_results: int = DEFAULT_LIST_LIMIT) -> str:
        """按 glob 列出工作区文件（模式与命中项都被限制在工作区内）。

        Args:
            pattern: glob 模式，如 **/*.py
            max_results: 最多返回多少条，默认 200
        """
        cap = max(1, _as_int(max_results, DEFAULT_LIST_LIMIT))
        try:
            paths = list(iter_files(host, pattern, limit=cap))
        except ValueError as exc:  # 非法模式（绝对路径 / 含 ..）回一条可读错误，
            return f"错误：{exc}"   # 而不是把异常抛给模型
        if not paths:
            return "（无匹配文件）"
        body = "\n".join(relative(host, path) for path in paths)
        if len(paths) >= cap:
            body += f"\n\n[提示] 已达上限 {cap} 条，可能还有更多；请用更精确的 pattern 缩小范围"
        return body

    ctx.tools.register(read_file)
    ctx.tools.register(write_file)
    ctx.tools.register(edit_file)
    ctx.tools.register(list_files)
