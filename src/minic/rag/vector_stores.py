"""向量库后端抽象：Chroma 与 Milvus Lite 统一接口。

:class:`VectorStore` 定义 RagStore 需要的五个操作（count / add / delete_by_doc_ids /
query / close），Chroma 与 Milvus Lite 各自实现。RagStore 只依赖抽象，不感知具体后端，
通过配置 ``rag.vector_backend`` 选择（chroma / milvus，默认 chroma 保持兼容）。

选择要点（用户拍板 Milvus Lite 嵌入式）：
- Milvus Lite 是 pymilvus 附带的单文件嵌入式库，无需服务端、无需 Docker；
- 主键用字符串（chunk_id），开 dynamic field 存任意标量元数据（Chroma metadata 同款自由 key）；
- 度量统一 COSINE：distance = 1 - 余弦相似度，与 Chroma ``hnsw:space=cosine`` 语义一致，
  RagStore 统一按 ``1.0 - distance`` 换算向量分，无需按后端特判。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_COLLECTION_NAME = "minic_docs"


@dataclass
class VectorHit:
    """单条向量检索命中。"""

    chunk_id: str
    metadata: dict[str, Any]  # 含 text 与全部标量元数据（不含 vector / id 本身）
    distance: float  # 余弦距离（0=完全一致）；相似度 = 1 - distance


class VectorStore(ABC):
    """RagStore 依赖的向量库最小接口。"""

    @abstractmethod
    def count(self) -> int:
        """返回当前向量条数。"""

    @abstractmethod
    def add(
        self,
        ids: list[str],
        texts: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """批量写入（同 id 覆盖）。metadatas 不含 text。"""

    @abstractmethod
    def delete_by_doc_ids(self, doc_ids: list[str]) -> None:
        """按 doc_id 删除其全部向量（幂等，不存在即无操作）。"""

    @abstractmethod
    def query(
        self,
        query_embedding: list[float],
        top_k: int,
        source: str | None = None,
    ) -> list[VectorHit]:
        """按查询向量检索，可选按 source 过滤，返回最多 top_k 条命中。"""

    @abstractmethod
    def export_all(self) -> list[dict[str, Any]]:
        """导出全部条目（迁移用）：每项含 id / text / vector / 其余标量元数据。"""

    @abstractmethod
    def close(self) -> None:
        """释放资源（Milvus 释放文件锁；Chroma 无操作）。"""


# ---------------------------------------------------------------------------
# Chroma 后端（默认，保持既有数据与行为不变）
# ---------------------------------------------------------------------------


class ChromaVectorStore(VectorStore):
    """基于 ``chromadb.PersistentClient`` 的实现。"""

    def __init__(self, data_dir: Path) -> None:
        import chromadb

        self._client = chromadb.PersistentClient(path=str(data_dir))
        self._collection = self._client.get_or_create_collection(
            name=_COLLECTION_NAME,
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )

    def count(self) -> int:
        """返回当前向量条数。"""
        return self._collection.count()

    def add(
        self,
        ids: list[str],
        texts: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """批量写入 Chroma。"""
        self._collection.add(
            ids=ids,
            documents=texts,
            embeddings=embeddings,
            metadatas=metadatas,
        )

    def delete_by_doc_ids(self, doc_ids: list[str]) -> None:
        """按 doc_id 删除（Chroma 按 metadata 过滤）。"""
        for doc_id in doc_ids:
            self._collection.delete(where={"doc_id": doc_id})

    def query(
        self,
        query_embedding: list[float],
        top_k: int,
        source: str | None = None,
    ) -> list[VectorHit]:
        """按查询向量检索。"""
        fetched = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where={"source": source} if source else None,
        )
        hits: list[VectorHit] = []
        for chunk_id, distance, metadata, text in zip(
            fetched.get("ids", [[]])[0],
            fetched.get("distances", [[]])[0],
            fetched.get("metadatas", [[]])[0],
            fetched.get("documents", [[]])[0],
        ):
            metadata = dict(metadata)
            metadata["text"] = text
            hits.append(VectorHit(chunk_id=chunk_id, metadata=metadata, distance=float(distance)))
        return hits

    def export_all(self) -> list[dict[str, Any]]:
        """导出全部条目（含向量，迁移用）。"""
        fetched = self._collection.get(include=["embeddings", "metadatas", "documents"])
        ids = fetched.get("ids")
        if not ids:
            return []
        documents = fetched.get("documents") or []
        embeddings = fetched.get("embeddings")
        if embeddings is None:
            embeddings = []
        metadatas = fetched.get("metadatas") or []
        rows: list[dict[str, Any]] = []
        for chunk_id, text, embedding, metadata in zip(ids, documents, embeddings, metadatas):
            rows.append(
                {
                    "id": chunk_id,
                    "text": text,
                    "vector": list(embedding),
                    **{key: value for key, value in dict(metadata).items()},
                }
            )
        return rows

    def close(self) -> None:
        """Chroma 无需显式释放。"""


# ---------------------------------------------------------------------------
# Milvus Lite 后端（嵌入式单文件库）
# ---------------------------------------------------------------------------


class MilvusVectorStore(VectorStore):
    """基于 ``pymilvus.MilvusClient`` 的本地嵌入式实现（Milvus Lite）。

    集合按需创建：首次写入时用第一批向量的实际维度建集合（维度以 embedding 模型
    实测输出为准，不依赖配置项）。检索前集合不存在时返回空，避免空库报错。
    """

    def __init__(self, data_dir: Path) -> None:
        from pymilvus import MilvusClient

        data_dir.mkdir(parents=True, exist_ok=True)  # MilvusClient 要求库文件父目录已存在
        self._db_path = data_dir / "milvus.db"
        self._client = MilvusClient(str(self._db_path))
        self._created = False

    def _ensure_collection(self, dimension: int) -> None:
        """按需创建集合（幂等）。"""
        if self._client.has_collection(_COLLECTION_NAME):
            self._created = True
            return
        self._client.create_collection(
            collection_name=_COLLECTION_NAME,
            dimension=dimension,
            metric_type="COSINE",
            id_type="string",
            max_length=512,  # chunk_id 长度
            enable_dynamic_field=True,
        )
        self._created = True

    def count(self) -> int:
        """返回当前向量条数。"""
        if not self._client.has_collection(_COLLECTION_NAME):
            return 0
        return int(self._client.get_collection_stats(_COLLECTION_NAME).get("row_count", 0))

    def add(
        self,
        ids: list[str],
        texts: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """批量写入（同 id 覆盖）。"""
        if not embeddings:
            return
        self._ensure_collection(len(embeddings[0]))
        rows = [
            {"id": chunk_id, "vector": embedding, "text": text, **metadata}
            for chunk_id, text, embedding, metadata in zip(ids, texts, embeddings, metadatas)
        ]
        self._client.upsert(_COLLECTION_NAME, data=rows)

    def delete_by_doc_ids(self, doc_ids: list[str]) -> None:
        """按 doc_id 删除（filter 表达式）。"""
        if not doc_ids or not self._client.has_collection(_COLLECTION_NAME):
            return
        quoted = ", ".join(json_quote(doc_id) for doc_id in doc_ids)
        self._client.delete(_COLLECTION_NAME, filter=f"doc_id in [{quoted}]")

    def query(
        self,
        query_embedding: list[float],
        top_k: int,
        source: str | None = None,
    ) -> list[VectorHit]:
        """按查询向量检索，可选按 source 过滤。"""
        if not self._client.has_collection(_COLLECTION_NAME):
            return []
        self._client.load_collection(_COLLECTION_NAME)  # 新进程重开库后集合处于 released，需加载
        filter_expr = f'source == {json_quote(source)}' if source else ""
        fetched = self._client.search(
            _COLLECTION_NAME,
            data=[query_embedding],
            limit=top_k,
            output_fields=["*"],
            filter=filter_expr,
        )
        hits: list[VectorHit] = []
        for hit in fetched[0]:
            entity = dict(hit.get("entity", {}))
            metadata = {
                key: value
                for key, value in entity.items()
                if key not in ("id", "vector")
            }
            if "text" not in metadata:
                continue
            hits.append(
                VectorHit(
                    chunk_id=entity["id"],
                    metadata=metadata,
                    distance=float(hit.get("distance", 0.0)),
                )
            )
        return hits

    def export_all(self) -> list[dict[str, Any]]:
        """导出全部条目（含向量，迁移用）。"""
        if not self._client.has_collection(_COLLECTION_NAME):
            return []
        self._client.load_collection(_COLLECTION_NAME)
        rows: list[dict[str, Any]] = []
        offset = 0
        batch_size = 1000
        while True:
            batch = self._client.query(
                _COLLECTION_NAME,
                filter="",
                output_fields=["*"],
                limit=batch_size,
                offset=offset,
            )
            if not batch:
                break
            for row in batch:
                rows.append(
                    {
                        "id": row["id"],
                        "text": row.get("text", ""),
                        "vector": row.get("vector", []),
                        **{
                            key: value
                            for key, value in row.items()
                            if key not in ("id", "vector", "text")
                        },
                    }
                )
            if len(batch) < batch_size:
                break
            offset += batch_size
        return rows

    def close(self) -> None:
        """释放 Milvus Lite 文件锁。"""
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - 释放阶段异常不影响主流程
            pass


def json_quote(value: str) -> str:
    """生成 Milvus filter 表达式中可用的字符串字面量（转义引号/反斜杠）。"""
    import json

    return json.dumps(value)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def create_vector_store(backend: str, data_dir: Path) -> VectorStore:
    """按后端名创建向量库实例。

    - chroma：数据目录下 ``chroma/`` 子目录（保持既有布局）；
    - milvus：数据目录下 ``milvus.db`` 单文件嵌入式库；
    - 其余未知值回退 Chroma（兼容手写配置里的历史值，不崩核心）。
    """
    if backend == "milvus":
        return MilvusVectorStore(data_dir)
    return ChromaVectorStore(data_dir / "chroma")
