"""G9 /rag/documents 接口测试：列表、source 过滤、分页、删除同步清理。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minic.core.config import AppSettings
from minic.core.server import create_app


HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def rag_client(settings: AppSettings, tmp_path: Path) -> TestClient:
    """使用临时 RAG 数据目录的文档接口客户端（双库隔离，入库默认进全局库）。"""
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
    )
    return TestClient(app)


def _ingest(client: TestClient, folder: Path, source: str | None = None) -> None:
    """入库一个文件夹，断言成功。"""
    payload = {"path": str(folder), "extensions": [".md"]}
    if source:
        payload["source"] = source
    response = client.post("/rag/ingest", headers=HEADERS, json=payload)
    assert response.status_code == 200
    assert response.json()["failed"] == []


def test_list_documents_after_ingest(rag_client: TestClient, tmp_path: Path) -> None:
    """入库后 GET /rag/documents 返回文档清单。"""
    folder = tmp_path / "kb-a"
    folder.mkdir()
    (folder / "guide.md").write_text("# 标题\n\n正文内容", encoding="utf-8")
    _ingest(rag_client, folder, source="kb-a")

    response = rag_client.get("/rag/documents", headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["next_cursor"] is None
    assert len(data["documents"]) == 1
    document = data["documents"][0]
    assert document["source"] == "kb-a"
    assert document["file_path"].endswith("guide.md")
    assert document["chunk_count"] >= 1
    assert document["chunk_ids"]
    assert document["hash"]
    assert document["embedding_model"]
    assert document["embedded_at"]


def test_list_documents_source_filter(rag_client: TestClient, tmp_path: Path) -> None:
    """按 source 过滤只返回对应知识库文档。"""
    folder_a = tmp_path / "kb-a"
    folder_a.mkdir()
    (folder_a / "a.md").write_text("# A\n\n内容 A", encoding="utf-8")
    folder_b = tmp_path / "kb-b"
    folder_b.mkdir()
    (folder_b / "b.md").write_text("# B\n\n内容 B", encoding="utf-8")
    _ingest(rag_client, folder_a, source="kb-a")
    _ingest(rag_client, folder_b, source="kb-b")

    filtered = rag_client.get("/rag/documents", headers=HEADERS, params={"source": "kb-a"}).json()
    assert len(filtered["documents"]) == 1
    assert filtered["documents"][0]["source"] == "kb-a"

    all_docs = rag_client.get("/rag/documents", headers=HEADERS).json()
    assert len(all_docs["documents"]) == 2


def test_list_documents_pagination(rag_client: TestClient, tmp_path: Path) -> None:
    """page_size=1 时 next_cursor 正确，可按游标翻页。"""
    folder = tmp_path / "kb"
    folder.mkdir()
    for index in range(3):
        (folder / f"doc{index}.md").write_text(f"# Doc {index}\n\n内容 {index}", encoding="utf-8")
    _ingest(rag_client, folder, source="kb")

    page1 = rag_client.get("/rag/documents", headers=HEADERS, params={"page_size": 1}).json()
    assert len(page1["documents"]) == 1
    assert page1["next_cursor"] == "1"

    page2 = rag_client.get(
        "/rag/documents",
        headers=HEADERS,
        params={"page_size": 1, "cursor": page1["next_cursor"]},
    ).json()
    assert len(page2["documents"]) == 1
    assert page2["next_cursor"] == "2"

    page3 = rag_client.get(
        "/rag/documents",
        headers=HEADERS,
        params={"page_size": 1, "cursor": page2["next_cursor"]},
    ).json()
    assert len(page3["documents"]) == 1
    assert page3["next_cursor"] is None

    ids = [page["documents"][0]["doc_id"] for page in (page1, page2, page3)]
    assert len(set(ids)) == 3


def test_delete_document_sync_cleanup(rag_client: TestClient, tmp_path: Path) -> None:
    """删除文档后状态计数减少、检索不再返回、重复删除 404。"""
    folder = tmp_path / "kb"
    folder.mkdir()
    (folder / "guide.md").write_text(
        "# MiniC 架构\n\nMiniC 使用 LangGraph 作为核心工作流引擎。\n\n"
        "## 检索\n\nMiniC 使用 Chroma 和 BM25 做混合检索。",
        encoding="utf-8",
    )
    _ingest(rag_client, folder, source="kb")

    status_before = rag_client.get("/rag/status", headers=HEADERS).json()
    assert status_before["total_documents"] == 1
    assert status_before["total_chunks"] >= 1

    document = rag_client.get("/rag/documents", headers=HEADERS).json()["documents"][0]
    doc_id = document["doc_id"]

    query_before = rag_client.get(
        "/rag/query", headers=HEADERS, params={"q": "LangGraph"}
    ).json()
    assert query_before["results"]

    response = rag_client.request("DELETE", f"/rag/documents/{doc_id}", headers=HEADERS)
    assert response.status_code == 204

    status_after = rag_client.get("/rag/status", headers=HEADERS).json()
    assert status_after["total_documents"] == 0
    assert status_after["total_chunks"] == 0

    query_after = rag_client.get(
        "/rag/query", headers=HEADERS, params={"q": "LangGraph"}
    ).json()
    assert query_after["results"] == []

    docs_after = rag_client.get("/rag/documents", headers=HEADERS).json()
    assert docs_after["documents"] == []

    repeated = rag_client.request("DELETE", f"/rag/documents/{doc_id}", headers=HEADERS)
    assert repeated.status_code == 404
    assert repeated.json()["error"]["detail"]["doc_id"] == doc_id


def test_delete_unknown_document_404(rag_client: TestClient) -> None:
    """删除不存在的文档返回 404。"""
    response = rag_client.request("DELETE", "/rag/documents/not-exist", headers=HEADERS)
    assert response.status_code == 404


def test_rag_documents_requires_auth(rag_client: TestClient) -> None:
    """未鉴权时列表与删除返回 401。"""
    assert rag_client.get("/rag/documents").status_code == 401
    assert rag_client.request("DELETE", "/rag/documents/x").status_code == 401


def test_delete_documents_by_path(rag_client: TestClient, tmp_path: Path) -> None:
    """按知识库路径批量删除：只删该路径的文档，删除后可重新入库。"""
    kb1 = tmp_path / "kb1"
    kb2 = tmp_path / "kb2"
    kb1.mkdir()
    kb2.mkdir()
    (kb1 / "a.md").write_text("# A\n\n内容 A。", encoding="utf-8")
    (kb2 / "b.md").write_text("# B\n\n内容 B。", encoding="utf-8")
    _ingest(rag_client, kb1)
    _ingest(rag_client, kb2)

    response = rag_client.request("DELETE", "/rag/documents", headers=HEADERS, params={"path": str(kb1)})
    assert response.status_code == 200
    assert response.json() == {"deleted": 1}

    documents = rag_client.get("/rag/documents", headers=HEADERS).json()["documents"]
    assert len(documents) == 1
    assert documents[0]["file_path"] == "b.md"

    missing = rag_client.request("DELETE", "/rag/documents", headers=HEADERS, params={"path": str(tmp_path / "nope")})
    assert missing.json() == {"deleted": 0}

    reingest = rag_client.post("/rag/ingest", headers=HEADERS, json={"path": str(kb1)})
    assert reingest.status_code == 200
    assert reingest.json()["ingested"] == 1
