"""Chroma + BM25 混合检索存储。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import chromadb
from rank_bm25 import BM25Okapi

from minic.core.config import AppSettings
from minic.rag.chunker import chunk_text
from minic.rag.embeddings import EmbeddingProvider
from minic.rag.parser import parse_markdown


@dataclass
class RagResult:
    """单条检索结果。"""

    doc_id: str
    source: str
    file_path: str
    title: str
    section: str
    score: float
    text: str
    page_file_path: str | None = None
    page: int | None = None
    scope: str = "global"  # 所属库作用域：global / project（双库检索标注用）

    def to_dict(self) -> dict[str, Any]:
        """转换为接口返回的字典（score 转原生 float，避免 numpy 类型进
        图状态后被 checkpointer 的 msgpack 序列化拒绝）。"""
        data = asdict(self)
        data["score"] = float(data.get("score") or 0.0)
        return data


@dataclass
class IngestResult:
    """入库结果。"""

    ingested: int
    skipped: int
    failed: list[dict[str, str]]


_COLLECTION_NAME = "minic_docs"
_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def _normalize_text(text: str) -> str:
    """统一小写并压缩空白。"""
    return " ".join(text.lower().split())


def tokenize(text: str) -> list[str]:
    """对中英文混合文本做简单词元化。"""
    text = _normalize_text(text)
    tokens: list[str] = []
    tokens.extend(_TOKEN_RE.findall(text))
    for cjk_segment in _CJK_RE.findall(text):
        if len(cjk_segment) <= 2:
            tokens.append(cjk_segment)
        else:
            tokens.extend(cjk_segment[index : index + 2] for index in range(len(cjk_segment) - 1))
    return tokens


def _sha256(value: str) -> str:
    """返回字符串的 sha256。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_markdown_file(path: Path) -> str:
    """读取 Markdown 文本，优先 UTF-8，失败时回退 GBK。"""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


def _chunk_search_text(chunk: dict[str, Any]) -> str:
    """返回用于检索的分块文本，包含标题、章节和正文。"""
    return " ".join(
        [
            str(chunk.get("title", "")),
            str(chunk.get("section", "")),
            str(chunk.get("text", "")),
        ]
    )


class RagStore:
    """负责入库、向量检索与 BM25 检索。"""

    def __init__(
        self,
        rag_data_dir: Path,
        settings: AppSettings,
        embedding_provider: EmbeddingProvider,
        scope_name: str = "global",
    ) -> None:
        self.rag_data_dir = rag_data_dir
        self.settings = settings
        self.embedding_provider = embedding_provider
        self.scope_name = scope_name  # 双库标注：global / project，query 结果带上 scope
        self.chroma_dir = rag_data_dir / "chroma"
        self.bm25_path = rag_data_dir / "bm25_index.json"
        self.metadata_path = rag_data_dir / "metadata.json"
        self.documents_path = rag_data_dir / "documents.json"
        self.chroma_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.chroma_dir))  # Chroma 向量库持久化
        self._collection = self._client.get_or_create_collection(
            name=_COLLECTION_NAME,
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )
        self._ingest_locks: dict[str, threading.Lock] = {}  # per-source 并发锁：同 source 同时只允许一个 ingest
        self._commit_lock = threading.Lock()  # 全局提交锁：Chroma/BM25/metadata 一致更新，避免不同 source 并发覆盖
        self._bm25_chunks: list[dict[str, Any]] = []
        self._bm25: BM25Okapi | None = None
        self._load_bm25()

    def _load_bm25(self) -> None:
        """从磁盘加载 BM25 索引数据。"""
        if not self.bm25_path.exists():
            self._bm25_chunks = []
            return
        data = json.loads(self.bm25_path.read_text(encoding="utf-8"))
        self._bm25_chunks = data.get("chunks", [])

    def _persist_bm25(self) -> None:
        """把 BM25 索引数据写入磁盘。"""
        self.bm25_path.parent.mkdir(parents=True, exist_ok=True)
        self.bm25_path.write_text(
            json.dumps({"chunks": self._bm25_chunks}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_documents(self) -> list[dict[str, Any]]:
        """读取文档元数据。"""
        if not self.metadata_path.exists():
            return []
        return json.loads(self.metadata_path.read_text(encoding="utf-8")).get("documents", [])

    def _persist_documents(self, documents: list[dict[str, Any]]) -> None:
        """写入文档元数据。"""
        self.rag_data_dir.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"documents": documents}, ensure_ascii=False, indent=2)
        self.metadata_path.write_text(content, encoding="utf-8")

    def inventory(self) -> list[dict[str, Any]]:
        """返回知识库文档清单，供总图路由判断。"""
        return [
            {
                "file_path": document.get("file_path"),
                "source": document.get("source"),
                "chunk_count": document.get("chunk_count"),
                "embedded_at": document.get("embedded_at"),
                "embedding_model": document.get("embedding_model"),
            }
            for document in self._load_documents()
        ]

    def list_documents(
        self,
        source: str | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """列出文档元数据，支持 source 过滤与数字游标分页。"""
        documents = [
            document
            for document in self._load_documents()
            if source is None or document.get("source") == source
        ]
        start = int(cursor) if cursor and cursor.isdigit() else 0
        page = documents[start : start + page_size]
        next_cursor = str(start + len(page)) if start + len(page) < len(documents) else None
        return page, next_cursor

    def delete_document(self, doc_id: str) -> bool:
        """删除文档并同步清理 Chroma、BM25 与 metadata.json。"""
        documents = self._load_documents()
        remaining = [document for document in documents if document.get("doc_id") != doc_id]
        if len(remaining) == len(documents):
            return False
        self._collection.delete(where={"doc_id": doc_id})
        new_chunks = [chunk for chunk in self._bm25_chunks if chunk.get("doc_id") != doc_id]
        if len(new_chunks) != len(self._bm25_chunks):
            self._bm25_chunks = new_chunks
            self._persist_bm25()
            self._bm25 = None
        self._persist_documents(remaining)
        return True

    def delete_documents_by_path(self, path: str) -> int:
        """删除指定路径（目录或文件）对应的全部已入库文档，返回删除数量。

        source 计算与 ingest_directory 一致：路径规范化后的 sha256 前 16 位，
        因此入过库的目录/文件可以按原路径精确反查。
        """
        root = Path(path).expanduser().resolve()
        effective_source = _sha256(os.path.normcase(str(root)))[:16]
        documents = self._load_documents()
        doc_ids = [
            document["doc_id"]
            for document in documents
            if document.get("source") == effective_source
        ]
        if not doc_ids:
            return 0
        remaining = [
            document for document in documents if document.get("doc_id") not in doc_ids
        ]
        with self._commit_lock:
            for doc_id in doc_ids:
                self._collection.delete(where={"doc_id": doc_id})
            self._bm25_chunks = [
                chunk for chunk in self._bm25_chunks if chunk.get("doc_id") not in doc_ids
            ]
            self._persist_bm25()
            self._bm25 = None
            self._persist_documents(remaining)
        return len(doc_ids)

    def _ensure_bm25(self) -> None:
        """当索引数据变化时重建 BM25 模型。"""
        if self._bm25 is None and self._bm25_chunks:
            self._bm25 = BM25Okapi([tokenize(_chunk_search_text(chunk)) for chunk in self._bm25_chunks])

    def _chunks_for_documents(
        self,
        file_path: Path,
        source: str,
        doc_id: str,
        file_hash: str,
    ) -> list[dict[str, Any]]:
        """解析单个 Markdown 文件并生成分块。"""
        text = _read_markdown_file(file_path)
        sections = parse_markdown(text, file_path)
        chunks: list[dict[str, Any]] = []
        chunk_index = 0
        for section in sections:
            for text_chunk in chunk_text(
                section.content,
                chunk_size=self.settings.rag.chunk_size,
                chunk_overlap=self.settings.rag.chunk_overlap,
            ):
                chunk_id = f"{doc_id}:{chunk_index}"
                chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "doc_id": doc_id,
                        "source": source,
                        "file_path": str(file_path),
                        "title": section.title,
                        "section": section.section,
                        "text": text_chunk,
                        "file_hash": file_hash,
                    }
                )
                chunk_index += 1
        return chunks

    def ingest_directory(
        self,
        path: str,
        extensions: list[str] | None = None,
        source: str | None = None,
    ) -> IngestResult:
        """入库一个 Markdown 文件或文件夹，返回统计结果。

        同一 source 同时只允许一个 ingest（规格书 8.4）：按 effective_source
        持有 per-source 锁，不同 source 可并行；现有调用方无需改动。
        """
        root = Path(path).expanduser().resolve()
        if not root.exists():
            raise ValueError(f"路径不存在: {root}")
        extension_set = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in (extensions or [".md"])}

        if root.is_file():
            candidates = [root]
        else:
            candidates = [candidate for candidate in root.rglob("*") if candidate.is_file()]
        files = [candidate for candidate in candidates if candidate.suffix.lower() in extension_set]

        effective_source = source or _sha256(os.path.normcase(str(root)))[:16]
        with self._ingest_locks.setdefault(effective_source, threading.Lock()):
            return self._ingest_directory_unlocked(root=root, files=files, effective_source=effective_source)

    def _ingest_directory_unlocked(self, root: Path, files: list[Path], effective_source: str) -> IngestResult:
        """在持有 effective_source 锁的前提下执行增量入库。

        文件解析/跳过判断在 per-source 锁内完成；索引与 metadata 提交阶段持全局提交锁并
        重新读取文档，避免不同 source 并发入库时互相覆盖 metadata（规格书 8.4「metadata 更新加锁」）。
        """
        documents = self._load_documents()
        source_documents = {doc["doc_id"]: doc for doc in documents if doc.get("source") == effective_source}
        changed_chunks: list[dict[str, Any]] = []
        changed_doc_ids: set[str] = set()
        new_documents: list[dict[str, Any]] = []
        ingested = 0
        skipped = 0
        failed: list[dict[str, str]] = []
        embedded_at = datetime.now().astimezone().isoformat()
        embedding_model = f"{self.settings.embedding.provider}/{self.settings.embedding.model}"

        for file_path in files:
            try:
                raw_bytes = file_path.read_bytes()
                file_hash = hashlib.sha256(raw_bytes).hexdigest()
                display_path = (
                    file_path.relative_to(root).as_posix()
                    if root.is_dir()
                    else file_path.name
                )
                doc_id = _sha256(f"{effective_source}:{display_path}")
                existing = source_documents.get(doc_id)
                if existing and existing.get("hash") == file_hash:
                    skipped += 1
                    continue
                chunks = self._chunks_for_documents(file_path, effective_source, doc_id, file_hash)
                if not chunks:
                    continue
                changed_chunks.extend(chunks)
                changed_doc_ids.add(doc_id)
                ingested += 1
                new_documents.append(
                    {
                        "doc_id": doc_id,
                        "source": effective_source,
                        "file_path": display_path,
                        "hash": file_hash,
                        "size": len(raw_bytes),
                        "mtime": datetime.fromtimestamp(file_path.stat().st_mtime).astimezone().isoformat(),
                        "chunk_count": len(chunks),
                        "chunk_ids": [chunk["chunk_id"] for chunk in chunks],
                        "embedding_model": embedding_model,
                        "embedded_at": embedded_at,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响其他文件
                failed.append(
                    {
                        "path": str(file_path),
                        "code": "PARSE_ERROR",
                        "message": str(exc),
                    }
                )

        if changed_chunks:
            embeddings = self.embedding_provider.embed_texts(  # 批量生成文档向量（不持提交锁，不同 source 可并行）
                [chunk["text"] for chunk in changed_chunks],
                text_type="document",
            )
            with self._commit_lock:
                current_documents = self._load_documents()  # 提交前重新读取，合并其他 source 的并发提交
                old_documents = [doc for doc in current_documents if doc.get("source") != effective_source]
                current_source = {
                    doc["doc_id"]: doc
                    for doc in current_documents
                    if doc.get("source") == effective_source
                }
                for doc_id in changed_doc_ids:
                    self._collection.delete(where={"doc_id": doc_id})
                self._collection.add(
                    ids=[chunk["chunk_id"] for chunk in changed_chunks],
                    documents=[chunk["text"] for chunk in changed_chunks],
                    embeddings=embeddings,
                    metadatas=[{key: value for key, value in chunk.items() if key != "text"} for chunk in changed_chunks],
                )
                self._bm25_chunks = [
                    chunk for chunk in self._bm25_chunks if chunk.get("doc_id") not in changed_doc_ids
                ] + changed_chunks
                self._persist_bm25()
                self._bm25 = None
                final_documents = (
                    old_documents
                    + [doc for doc_id, doc in current_source.items() if doc_id not in changed_doc_ids]
                    + new_documents
                )
                self._persist_documents(final_documents)
        elif not self.metadata_path.exists():
            with self._commit_lock:
                self._persist_documents(self._load_documents())

        return IngestResult(ingested=ingested, skipped=skipped, failed=failed)

    def _bm25_scores(self, query: str, top_k: int, source: str | None) -> list[dict[str, Any]]:
        """计算 BM25 检索结果。"""
        self._ensure_bm25()
        if not self._bm25_chunks:
            return []
        candidate_chunks = (
            self._bm25_chunks
            if source is None
            else [chunk for chunk in self._bm25_chunks if chunk.get("source") == source]
        )
        if not candidate_chunks:
            return []
        indexes = [index for index, chunk in enumerate(self._bm25_chunks) if chunk in candidate_chunks]
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        scores = self._bm25.get_scores(query_tokens)
        ranked = sorted(indexes, key=lambda index: scores[index], reverse=True)[:top_k]
        if not ranked:
            return []
        min_score = min(scores[index] for index in ranked)
        max_score = max(scores[index] for index in ranked)
        span = max_score - min_score
        return [
            {
                "chunk": self._bm25_chunks[index],
                "score": ((scores[index] - min_score) / span) if span > 0 else 1.0,
            }
            for index in ranked
        ]

    def query(self, query: str, top_k: int | None = None, source: str | None = None) -> list[RagResult]:
        """执行向量与 BM25 混合检索。"""
        size = top_k or self.settings.rag.top_k
        where = {"source": source} if source else None
        vector_results: list[tuple[dict[str, Any], float]] = []
        if self._collection.count() > 0:
            query_embeddings = self.embedding_provider.embed_texts([query], text_type="query")  # 问题向量用于相似度检索
            fetched = self._collection.query(
                query_embeddings=query_embeddings,
                n_results=max(size * 4, 10),
                where=where,
            )
            ids = fetched.get("ids", [[]])[0]
            distances = fetched.get("distances", [[]])[0]
            metadatas = fetched.get("metadatas", [[]])[0]
            documents = fetched.get("documents", [[]])[0]
            for chunk_id, distance, metadata, text in zip(ids, distances, metadatas, documents):
                metadata = dict(metadata)
                metadata["text"] = text
                vector_results.append((metadata, 1.0 - float(distance)))

        query_tokens = set(tokenize(query))
        if not query_tokens:
            return []
        bm25_candidates = (
            self._bm25_chunks
            if source is None
            else [chunk for chunk in self._bm25_chunks if chunk.get("source") == source]
        )
        bm25_hit_ids = {
            chunk["chunk_id"]
            for chunk in bm25_candidates
            if query_tokens & set(tokenize(_chunk_search_text(chunk)))
        }
        bm25_results = self._bm25_scores(query, top_k=max(size * 4, 10), source=source)
        merged: dict[str, dict[str, Any]] = {}
        for metadata, score in vector_results:
            chunk_id = metadata["chunk_id"]
            merged.setdefault(
                chunk_id,
                {"metadata": metadata, "vector_score": 0.0, "bm25_score": 0.0},
            )
            merged[chunk_id]["vector_score"] = max(merged[chunk_id]["vector_score"], score)
        for item in bm25_results:
            chunk = item["chunk"]
            chunk_id = chunk["chunk_id"]
            merged.setdefault(
                chunk_id,
                {"metadata": chunk, "vector_score": 0.0, "bm25_score": 0.0},
            )
            merged[chunk_id]["bm25_score"] = max(merged[chunk_id]["bm25_score"], item["score"])

        vector_weight = self.settings.rag.vector_weight
        bm25_weight = self.settings.rag.bm25_weight
        results = []
        for chunk_id, item in merged.items():
            if chunk_id not in bm25_hit_ids:
                continue
            metadata = item["metadata"]
            score = vector_weight * item["vector_score"] + bm25_weight * item["bm25_score"]
            results.append(
                RagResult(
                    doc_id=metadata["doc_id"],
                    source=metadata["source"],
                    file_path=metadata["file_path"],
                    title=metadata["title"],
                    section=metadata["section"],
                    score=round(score, 4),
                    text=metadata["text"],
                    scope=self.scope_name,
                )
            )
        results.sort(key=lambda result: result.score, reverse=True)  # 按混合分数降序返回
        return results[:size]
