"""S1 入库与检索测试。"""

from pathlib import Path
import json

from fastapi.testclient import TestClient


HEADERS = {"Authorization": "Bearer test-token"}


def test_ingest_markdown_folder(client: TestClient, markdown_dir: Path) -> None:
    """Markdown 文件夹可以入库。"""
    response = client.post(
        "/rag/ingest",
        headers=HEADERS,
        json={"path": str(markdown_dir), "extensions": [".md"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ingested"] == 1
    assert data["skipped"] == 0
    assert data["failed"] == []


def test_query_returns_source(client: TestClient, markdown_dir: Path) -> None:
    """检索结果包含文件路径与章节。"""
    client.post(
        "/rag/ingest",
        headers=HEADERS,
        json={"path": str(markdown_dir), "extensions": [".md"]},
    )
    response = client.get(
        "/rag/query",
        headers=HEADERS,
        params={"q": "LangGraph 的作用是什么", "top_k": 3},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "LangGraph 的作用是什么"
    assert data["results"]
    result = data["results"][0]
    assert result["file_path"].endswith("guide.md")
    assert result["section"]


def test_query_requires_auth(client: TestClient) -> None:
    """未鉴权时检索返回 401。"""
    response = client.get("/rag/query", params={"q": "test"})
    assert response.status_code == 401


def test_reingest_skips_unchanged(client: TestClient, markdown_dir: Path) -> None:
    """重复入库同一文件夹时，未变化文件应全部跳过。"""
    payload = {"path": str(markdown_dir), "extensions": [".md"]}
    first = client.post("/rag/ingest", headers=HEADERS, json=payload)
    assert first.json()["ingested"] == 1
    second = client.post("/rag/ingest", headers=HEADERS, json=payload)
    assert second.json()["ingested"] == 0
    assert second.json()["skipped"] == 1


def test_metadata_json_is_written(
    client: TestClient,
    markdown_dir: Path,
    temp_dirs: dict,
) -> None:
    """入库后 rag-data 下应生成 metadata.json（单库）。"""
    response = client.post(
        "/rag/ingest",
        headers=HEADERS,
        json={"path": str(markdown_dir), "extensions": [".md"]},
    )
    assert response.status_code == 200
    metadata_path = temp_dirs["rag_data"] / "metadata.json"
    assert metadata_path.exists()
    data = json.loads(metadata_path.read_text(encoding="utf-8"))
    document = data["documents"][0]
    assert document["chunk_ids"]
    assert document["embedding_model"]


def test_unrelated_query_returns_empty(client: TestClient, markdown_dir: Path) -> None:
    """资料库没有答案时应返回空结果。"""
    client.post(
        "/rag/ingest",
        headers=HEADERS,
        json={"path": str(markdown_dir), "extensions": [".md"]},
    )
    response = client.get(
        "/rag/query",
        headers=HEADERS,
        params={"q": "量子菠萝星球的定义是什么", "top_k": 3},
    )
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_query_matches_section_title(client: TestClient, tmp_path: Path) -> None:
    """正文不含关键词但章节标题含关键词时，也应能检索到。"""
    folder = tmp_path / "titledocs"
    folder.mkdir()
    (folder / "linkedin.md").write_text(
        "# 文档总览\n\n"
        "## LinkedIn（领英）\n\n"
        "这里是关于职场社交网络平台的内容。\n\n"
        "## 三者关系与行动建议\n\n"
        "LinkedIn、B2B、ROI 三者之间存在协作关系。",
        encoding="utf-8",
    )
    client.post(
        "/rag/ingest",
        headers=HEADERS,
        json={"path": str(folder), "extensions": [".md"]},
    )
    response = client.get(
        "/rag/query",
        headers=HEADERS,
        params={"q": "LinkedIn 是什么", "top_k": 10},
    )
    assert response.status_code == 200
    sections = [result["section"] for result in response.json()["results"]]
    assert any("LinkedIn（领英）" in section for section in sections)
