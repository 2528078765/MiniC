"""Milvus Lite 向量后端测试（vector_backend=milvus 全链路）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minic.core.config import AppSettings
from minic.core.server import create_app
from minic.rag.embeddings import MockEmbeddingProvider
from minic.rag.store import RagStore

try:
    import pymilvus  # noqa: F401

    HAS_MILVUS = True
except ImportError:
    HAS_MILVUS = False

pytestmark = pytest.mark.skipif(
    not HAS_MILVUS,
    reason="未安装 pymilvus/milvus-lite",
)


def _make_store(rag_data_dir: Path, backend: str = "milvus") -> RagStore:
    """构造 Milvus 后端的 RagStore。"""
    settings = AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 16},
        rag={"chunk_size": 200, "chunk_overlap": 40, "top_k": 5, "vector_backend": backend},
    )
    return RagStore(rag_data_dir, settings, MockEmbeddingProvider(dimension=16))


def _make_client(tmp_path: Path, rag: dict | None = None) -> TestClient:
    """构造 milvus 后端的 API 客户端。"""
    settings = AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 16},
        rag={
            "chunk_size": 200,
            "chunk_overlap": 40,
            "top_k": 5,
            "vector_backend": "milvus",
            **(rag or {}),
        },
    )
    return TestClient(
        create_app(
            settings=settings,
            token="test-token",
            project_root=Path.cwd(),
            rag_data_dir=tmp_path / "rag-data",
            short_memory_dir=tmp_path / "memory" / "short_memory",
            skills_global_dir=tmp_path / "skills_global",
            skills_project_dir=tmp_path / ".minic" / "skills",
        )
    )


HEADERS = {"Authorization": "Bearer test-token"}


def test_milvus_via_api_ingest_query(tmp_path: Path) -> None:
    """经 API 使用 Milvus 后端：入库、检索、未命中。"""
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "guide.md").write_text(
        "# MiniC 架构\n\n"
        "MiniC 使用 LangGraph 作为核心工作流引擎。\n\n"
        "## 检索\n\n"
        "MiniC 使用 Milvus 和 BM25 做混合检索。",
        encoding="utf-8",
    )
    with _make_client(tmp_path) as client:
        result = client.post("/rag/ingest", headers=HEADERS, json={"path": str(folder), "extensions": [".md"]})
        assert result.status_code == 200
        assert result.json()["ingested"] == 1

        query = client.get("/rag/query", headers=HEADERS, params={"q": "LangGraph 的作用是什么", "top_k": 3})
        assert query.status_code == 200
        assert query.json()["results"]
        assert query.json()["results"][0]["file_path"].endswith("guide.md")

        assert (tmp_path / "rag-data" / "milvus.db").exists()


def test_milvus_backend_put_settings(tmp_path: Path) -> None:
    """PUT /settings 可保存 vector_backend（全局 rag 段）。"""
    with _make_client(tmp_path) as client:
        response = client.put(
            "/settings",
            headers=HEADERS,
            json={"scope": "global", "rag": {"vector_backend": "milvus"}},
        )
        assert response.status_code == 204  # PUT /settings 成功返回 204 No Content


def test_milvus_ingest_and_query(tmp_path: Path) -> None:
    """Milvus 后端入库后可检索到对应来源。"""
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "guide.md").write_text(
        "# MiniC 架构\n\n"
        "MiniC 使用 LangGraph 作为核心工作流引擎。\n\n"
        "## 检索\n\n"
        "MiniC 使用 Milvus 和 BM25 做混合检索，回答会带文件路径和章节来源。",
        encoding="utf-8",
    )
    store = _make_store(tmp_path / "rag-data")
    try:
        result = store.ingest_directory(str(folder), extensions=[".md"])
        assert result.ingested == 1
        assert result.failed == []

        hits = store.query("LangGraph 的作用是什么", top_k=3)
        assert hits, "Milvus 后端检索应有结果"
        assert hits[0].file_path.endswith("guide.md")
        assert hits[0].section

        # 未命中查询返回空
        assert store.query("量子菠萝星球的定义", top_k=3) == []
    finally:
        store.close()


def test_milvus_reingest_skips_and_persists(tmp_path: Path) -> None:
    """重复入库跳过未变化文件，且数据跨实例持久化。"""
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "guide.md").write_text("# 主题\n\n内容内容内容", encoding="utf-8")

    data_dir = tmp_path / "rag-data"
    first = _make_store(data_dir)
    try:
        assert first.ingest_directory(str(folder), extensions=[".md"]).ingested == 1
    finally:
        first.close()

    second = _make_store(data_dir)  # 重新打开（模拟核心重启）
    try:
        again = second.ingest_directory(str(folder), extensions=[".md"])
        assert again.ingested == 0
        assert again.skipped == 1
        assert second.query("主题", top_k=3)
    finally:
        second.close()


def test_milvus_delete_document(tmp_path: Path) -> None:
    """删除文档后向量、BM25 与 metadata 同步清理。"""
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.md").write_text("# AAA\n\naaa 内容", encoding="utf-8")
    store = _make_store(tmp_path / "rag-data")
    try:
        store.ingest_directory(str(folder), extensions=[".md"])
        doc_id = store.inventory()[0]["file_path"]
        # 用 delete_documents_by_path 按源路径删除
        deleted = store.delete_documents_by_path(str(folder))
        assert deleted == 1
        assert store.query("aaa", top_k=3) == []
    finally:
        store.close()


def test_milvus_backend_missing_db_file_creates(tmp_path: Path) -> None:
    """首次使用自动创建 milvus.db 单文件库。"""
    data_dir = tmp_path / "rag-data"
    store = _make_store(data_dir)
    try:
        assert (data_dir / "milvus.db").exists()
    finally:
        store.close()
