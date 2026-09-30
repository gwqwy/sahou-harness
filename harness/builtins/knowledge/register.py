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

import json
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
        """在本地知识库里检索与 query 最相关的片段，返回原文与来源。

        回答「文档里怎么写的 / 之前是怎么约定的」这类问题前先用它，不要凭记忆作答。
        引用了检索结果时，回复里必须用 [编号]（来源: 文件名） 标注出处。

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
        lines = []
        for i, hit in enumerate(hits):
            meta = hit.get("metadata") or {}
            source = str(meta.get("source") or "（未知）")
            lines.append(f"[{i + 1}]（来源: {source}，相关度 {hit.get('score', 0):.3f}）\n"
                         f"{hit.get('text', '')}")
        lines.append("（回答引用以上内容时，请用 [编号]（来源: 文件名） 标注出处。）")
        return "\n\n".join(lines)

    def _index_target(target: Path, kb) -> str:
        """对一个已解析的文件/目录执行索引（工具与桌面端服务共用）。"""
        from harness.workspace import read_text_file

        if target.is_file():
            files = [target]
        else:
            from harness.workspace import iter_files, relative

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

    def index_knowledge(path: str = "") -> str:
        """把工作区里的文件或目录索引进知识库（只处理文本类文件）。

        同一份文件重复索引会重复占位，建议入库前先确认没入过。

        Args:
            path: 工作区相对路径；目录则递归处理常见文本文件
        """
        from harness.workspace import safe_path

        try:
            target = safe_path(host, path) if str(path or "").strip() else Path(host.workspace)
        except (ValueError, OSError) as exc:
            return f"错误：{exc}"
        try:
            kb = _kb()
        except Exception as exc:  # noqa: BLE001
            return f"错误：知识库不可用（{type(exc).__name__}: {exc}）"
        return _index_target(target, kb)

    def index_absolute(path: str) -> str:
        """把一个**绝对路径**索引进知识库（仅限界面用户亲自选择的目录）。

        agent 工具拿不到它（不注册进 ctx.tools），因此不构成绕过路径监狱的通道。
        """
        from harness.workspace import read_text_file

        target = Path(str(path or "").strip())
        if not target.exists():
            return f"错误：路径不存在: {target}"
        try:
            kb = _kb()
        except Exception as exc:  # noqa: BLE001
            return f"错误：知识库不可用（{type(exc).__name__}: {exc}）"

        files = [target] if target.is_file() else [
            p for p in sorted(target.rglob("*"))
            if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES][:MAX_INDEX_FILES]

        added_files = 0
        added_chunks = 0
        failures: list[str] = []
        for item in files:
            text = read_text_file(item)
            if text is None:
                continue
            try:
                count = kb.add_text(text, {"source": item.name})
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{item.name}: {type(exc).__name__}")
                break
            if count:
                added_files += 1
                added_chunks += count
        if added_chunks == 0:
            return ("错误：没有入库任何内容"
                    + (f"（失败于 {failures[0]}）" if failures else "（没有可索引的文本文件）"))
        return f"已索引 {added_files} 个文件、{added_chunks} 个片段（知识库共 {len(kb.store)} 段）"

    def clear_knowledge() -> str:
        """清空知识库：删掉持久化索引并重置内存库（下次检索时惰性重建）。"""
        state["kb"] = None
        index_dir = Path(host.profile.root) / str(settings.get("dir") or "knowledge")
        removed = 0
        if index_dir.is_dir():
            for item in index_dir.glob("index.npz*"):  # numpy 存 index.npz；faiss 另带 .meta.json 等伴生文件
                try:
                    item.unlink()
                    removed += 1
                except OSError:
                    pass
        return f"已清空知识库（删除 {removed} 个索引文件）"

    def _sources_payload() -> list[dict]:
        """按来源文件汇总片段数（内存库优先，否则读持久化索引，不构建 KB）。"""
        counts: dict[str, int] = {}
        if state["kb"] is not None:
            for meta in state["kb"].store.metadata:
                name = str((meta or {}).get("source") or "（未知来源）")
                counts[name] = counts.get(name, 0) + 1
            return [{"source": k, "chunks": v} for k, v in sorted(counts.items())]
        persist = (Path(host.profile.root) / str(settings.get("dir") or "knowledge")
                   / "index.npz")
        if not persist.is_file():
            return []
        try:
            payload = json.loads(persist.read_text(encoding="utf-8"))
            metas = payload.get("metadata") if isinstance(payload, dict) else None
            for meta in (metas if isinstance(metas, list) else []):
                if not isinstance(meta, dict):
                    continue
                name = str(meta.get("source") or "（未知来源）")
                counts[name] = counts.get(name, 0) + 1
        except (ValueError, OSError):
            return []
        return [{"source": k, "chunks": v} for k, v in sorted(counts.items())]

    def knowledge_sources() -> list[dict]:
        """列出已索引的来源文件与各自片段数（供界面做细粒度管理）。"""
        return _sources_payload()

    def knowledge_remove_source(name: str) -> str:
        """从知识库里删除某个来源文件的全部片段（其余来源保留），并落盘。"""
        target = str(name or "").strip()
        if not target:
            return "错误：缺少来源文件名"
        kb = state["kb"]
        if kb is None:
            # 惰性构建需要模型（embeddings）；无索引文件时直接报告为空
            persist = (Path(host.profile.root) / str(settings.get("dir") or "knowledge")
                       / "index.npz")
            if not persist.is_file():
                return "知识库还是空的，没有可删除的来源"
            try:
                kb = _kb()
            except Exception as exc:  # noqa: BLE001
                return f"错误：知识库不可用（{type(exc).__name__}: {exc}）"
        store = kb.store
        keep = [i for i, meta in enumerate(store.metadata)
                if str(meta.get("source") or "") != target]
        removed = len(store.texts) - len(keep)
        if removed == 0:
            return f"错误：知识库里没有来源为「{target}」的片段"
        store.texts = [store.texts[i] for i in keep]
        store.metadata = [store.metadata[i] for i in keep]
        if store.vectors is not None:
            store.vectors = store.vectors[keep] if keep else None
        try:
            kb.save()
        except Exception as exc:  # noqa: BLE001 —— 落盘失败也先报告删除结果
            return (f"已从内存移除 {removed} 个片段，但落盘失败"
                    f"（{type(exc).__name__}: {exc}），重启后可能恢复")
        return f"已删除来源「{target}」的 {removed} 个片段（知识库共 {len(store)} 段）"

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

    # -- 宿主服务：桌面端「📚 知识库」面板通过 host.service("knowledge") 消费 ----
    def status() -> dict:
        index_dir = Path(host.profile.root) / str(settings.get("dir") or "knowledge")
        info: dict = {"backend": str(settings.get("backend") or "numpy"),
                      "dir": str(index_dir),
                      "embedding_model": str(settings.get("embedding_model") or "")}
        if state["kb"] is not None:
            info["chunks"] = len(state["kb"].store)
            return info
        # 不构建 KB（不要求模型可用）：尽力从磁盘上的持久化索引数一下条数
        persist = index_dir / "index.npz"
        if persist.is_file():
            try:
                payload = json.loads(persist.read_text(encoding="utf-8"))
                texts = payload.get("texts") if isinstance(payload, dict) else None
                info["chunks"] = len(texts) if isinstance(texts, list) else -1
            except (ValueError, OSError):
                info["chunks"] = -1
        elif index_dir.is_dir() and any(index_dir.glob("index.npz*")):
            info["chunks"] = -1  # faiss 等二进制格式：有条数不明，重建后可知
        else:
            info["chunks"] = 0
        return info

    ctx.provide("knowledge", {
        "status": status,
        "index": index_knowledge,       # 工作区内路径（走监狱）
        "index_absolute": index_absolute,  # 桌面端用户亲自选择的绝对路径
        "clear": clear_knowledge,
        "sources": knowledge_sources,          # 细粒度管理：来源清单
        "remove_source": knowledge_remove_source,  # 删除单个来源的全部片段
    })
