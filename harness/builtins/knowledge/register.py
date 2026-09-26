"""知识库插件（RAG）：让 agent 能查自己的资料，而不是全靠模型记忆。

nanoagent 早就有完整的 RAG 子包（分块 / numpy+faiss 双后端向量库 / 检索 / LLM 重排），
harness 却一直没接 —— agent 面对「我们内部文档里怎么写的」这类问题只能瞎猜。

配置（config.json，可选）：

    "knowledge": {
      "enabled": true,
      "dir": "knowledge",            // 相对 profile 目录，存索引
      "backend": "numpy",            // 或 "faiss"（需 nanoagent[faiss]）
      "embedding_model": "text-embedding-3-small"
    }

用的是 OpenAI 兼容的 ``/embeddings`` 接口；因此索引与检索都需要模型可用。
索引器**惰性构建**：不查不建，避免启动时就要求网络。

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
"""

from __future__ import annotations

from pathlib import Path

TEXT_SUFFIXES = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".rst", ".csv", ".sahou"}
MAX_INDEX_FILES = 200


def register(ctx) -> None:
    host = ctx.host
    settings = host.profile.load_config().get("knowledge")
    settings = settings if isinstance(settings, dict) else {}

    if settings.get("enabled") is False:
        ctx.skipped.append("disabled: knowledge.enabled=false")
        return

    state: dict = {"kb": None}

    def _kb():
        if state["kb"] is None:
            runtime = host.service("models_runtime") or {}
            pool = runtime.get("pool") or {}
            llm = pool.get(runtime.get("current") or "") or (next(iter(pool.values())) if pool else None)
            if llm is None:
                raise RuntimeError("没有可用模型：知识库向量化需要 /embeddings 接口")
            from nanoagent.rag import KnowledgeBase

            index_dir = Path(host.profile.root) / str(settings.get("dir") or "knowledge")
            index_dir.mkdir(parents=True, exist_ok=True)
            state["kb"] = KnowledgeBase(
                llm=llm,
                embedding_model=settings.get("embedding_model"),
                persist_path=index_dir / "index.npz",
                backend=str(settings.get("backend") or "numpy"),
            )
        return state["kb"]

    def search_knowledge(query: str, k: int = 4) -> str:
        """在本地知识库里检索与 query 最相关的片段，返回原文。

        回答「文档里怎么写的 / 之前是怎么约定的」这类问题前先用它，不要凭记忆作答。

        Args:
            query: 检索问题
            k: 返回几条片段，默认 4
        """
        text = str(query or "").strip()
        if not text:
            return "错误：query 不能为空"
        try:
            kb = _kb()
        except Exception as exc:  # noqa: BLE001
            return f"错误：知识库不可用（{type(exc).__name__}: {exc}）"
        if len(kb.store) == 0:
            return "知识库还是空的：先用 index_knowledge 把文档索引进库。"
        try:
            hits = kb.query(text, k=max(1, int(k)))
        except Exception as exc:  # noqa: BLE001 —— 多为网络/embeddings 接口问题
            return f"错误：检索失败（{type(exc).__name__}: {exc}）"
        if not hits:
            return "知识库中没有找到相关内容。"
        return "\n\n".join(
            f"[{i + 1}] (相关度 {hit.get('score', 0):.3f}) {hit.get('text', '')}"
            for i, hit in enumerate(hits)
        )

    def index_knowledge(path: str = "") -> str:
        """把工作区里的文件或目录索引进知识库（只处理文本类文件）。

        同一份文件重复索引会重复占位，建议入库前先确认没入过。

        Args:
            path: 工作区相对路径；目录则递归处理常见文本文件
        """
        from harness.workspace import iter_files, read_text_file, relative, safe_path

        try:
            target = safe_path(host, path) if str(path or "").strip() else Path(host.workspace)
        except (ValueError, OSError) as exc:
            return f"错误：{exc}"
        try:
            kb = _kb()
        except Exception as exc:  # noqa: BLE001
            return f"错误：知识库不可用（{type(exc).__name__}: {exc}）"

        if target.is_file():
            files = [target]
        else:
            try:
                pattern = f"{relative(host, target)}/**/*" if target != Path(host.workspace) else "**/*"
                files = [p for p in iter_files(host, pattern, limit=MAX_INDEX_FILES)
                         if p.suffix.lower() in TEXT_SUFFIXES]
            except (ValueError, OSError) as exc:
                return f"错误：{exc}"

        added_files = 0
        added_chunks = 0
        failures: list[str] = []
        for item in files:
            if item.suffix.lower() not in TEXT_SUFFIXES:
                continue
            text = read_text_file(item)
            if text is None:
                continue
            try:
                count = kb.add_text(text, {"source": item.name})
            except Exception as exc:  # noqa: BLE001 —— 单个文件失败不放弃整批
                failures.append(f"{item.name}: {type(exc).__name__}")
                break  # 多半是网络/密钥问题，继续试也只会一路失败
            if count:
                added_files += 1
                added_chunks += count

        if added_chunks == 0:
            return ("错误：没有入库任何内容"
                    + (f"（失败于 {failures[0]}）" if failures else "（没有可索引的文本文件）"))
        summary = f"已索引 {added_files} 个文件、{added_chunks} 个片段（知识库共 {len(kb.store)} 段）"
        if failures:
            summary += f"；中断于 {failures[0]}"
        return summary

    ctx.tools.register(search_knowledge)
    ctx.tools.register(index_knowledge)

    def command_knowledge(args: str) -> str:
        try:
            kb = _kb()
        except Exception as exc:  # noqa: BLE001
            return f"知识库未就绪：{exc}"
        index_dir = Path(host.profile.root) / str(settings.get("dir") or "knowledge")
        return f"知识库共 {len(kb.store)} 个片段（索引目录: {index_dir}）"

    ctx.commands.register("knowledge", command_knowledge, "查看知识库状态")
