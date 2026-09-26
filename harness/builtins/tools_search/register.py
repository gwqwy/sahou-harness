"""搜索工具插件：grep 式的内容检索。

为什么需要它：此前只有 ``list_files``（列路径）+ ``read_file``（读单个文件），
模型在一个几十上百文件的仓库里等于瞎子 —— 想知道"哪里定义了这个函数"，
只能一个个文件读过去。有了它，agent 才能先定位再精读。

路径安全复用 :mod:`harness.workspace`（模式与命中项都限制在工作区内，
并跳过版本/构建产物目录）。

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
"""

from __future__ import annotations

import re

from harness.workspace import iter_files, read_text_file, relative

DEFAULT_MAX_RESULTS = 60
MAX_RESULTS_CAP = 500
# 单行匹配内容的截断长度：日志行可能极长，回给模型时没必要原样带上
MAX_LINE_CHARS = 240


def _as_int(value, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def register(ctx) -> None:
    host = ctx.host

    def search_text(pattern: str, glob: str = "**/*", max_results: int = DEFAULT_MAX_RESULTS,
                    ignore_case: bool = True, regex: bool = False) -> str:
        """在工作区文件内容里搜索文本，返回「相对路径:行号: 该行内容」。

        用于定位定义、调用点、配置项等。结果会自动跳过 .git/__pycache__/构建产物
        与二进制文件。

        Args:
            pattern: 要搜索的文本；regex=true 时按正则解释
            glob: 限定文件范围，如 **/*.py、src/**
            max_results: 最多返回多少条命中，默认 60
            ignore_case: 是否忽略大小写，默认 true
            regex: pattern 是否为正则表达式，默认 false（按字面量搜索）
        """
        raw = str(pattern or "")
        if not raw:
            return "错误：pattern 不能为空"
        flags = re.IGNORECASE if ignore_case else 0
        try:
            compiled = re.compile(raw if regex else re.escape(raw), flags)
        except re.error as exc:
            return f"错误：正则表达式无效（{exc}）"
        cap = min(max(1, _as_int(max_results, DEFAULT_MAX_RESULTS)), MAX_RESULTS_CAP)

        hits: list[str] = []
        scanned = 0
        skipped_binary = 0
        truncated = False
        try:
            candidates = iter_files(host, glob)
            for path in candidates:
                text = read_text_file(path)
                if text is None:
                    skipped_binary += 1
                    continue
                scanned += 1
                rel = relative(host, path)
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if not compiled.search(line):
                        continue
                    shown = line.strip()
                    if len(shown) > MAX_LINE_CHARS:
                        shown = shown[:MAX_LINE_CHARS] + "…"
                    hits.append(f"{rel}:{lineno}: {shown}")
                    if len(hits) >= cap:
                        truncated = True
                        break
                if truncated:
                    break
        except ValueError as exc:  # 非法 glob（绝对路径 / 含 ..）→ 回可读错误
            return f"错误：{exc}"

        if not hits:
            return f"（无匹配：扫描 {scanned} 个文件，跳过 {skipped_binary} 个二进制/超大文件）"
        body = "\n".join(hits)
        note = (f"\n\n[提示] 已达上限 {cap} 条，可能还有更多；"
                f"请用更精确的 pattern 或 glob 缩小范围") if truncated else ""
        return f"{body}{note}\n\n（扫描 {scanned} 个文件，命中 {len(hits)} 处）"

    ctx.tools.register(search_text)
