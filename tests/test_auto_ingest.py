"""G12 启动自动入库测试：配置生效、后台不阻塞、增量幂等、per-source 锁、失败容忍、状态字段。"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from minic.core.config import AppSettings
from minic.core.server import create_app
from minic.rag.embeddings import MockEmbeddingProvider
from minic.rag.store import RagStore

HEADERS = {"Authorization": "Bearer test-token"}


def make_settings(tmp_path: Path, auto_ingest_paths: list[str]) -> AppSettings:
    """带 mock 模型/mock embedding 与指定 auto_ingest_paths 的测试配置。"""
    return AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 64},
        rag={
            "chunk_size": 500,
            "chunk_overlap": 100,
            "top_k": 5,
            "bm25_weight": 0.45,
            "vector_weight": 0.55,
            "auto_ingest_paths": auto_ingest_paths,
        },
    )


def make_client(tmp_path: Path, settings: AppSettings) -> TestClient:
    """用临时项目根与临时 RAG 数据目录创建客户端（双库隔离，启动自动入库走项目库）。"""
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=tmp_path / "project",
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "memory" / "short_memory",
    )
    return TestClient(app)


def make_kb(tmp_path: Path, name: str = "kb") -> Path:
    """构造含 3 个 Markdown 文件的知识库目录。"""
    folder = tmp_path / name
    folder.mkdir()
    for index in range(3):
        (folder / f"doc{index}.md").write_text(
            f"# 文档 {index}\n\n这是第 {index} 篇文档，包含启动自动入库测试内容。",
            encoding="utf-8",
        )
    return folder


def read_auto_ingest_log(tmp_path: Path) -> list[dict]:
    """读取 <project>/.minic/logs/auto_ingest.jsonl 的全部记录。"""
    log_path = tmp_path / "project" / ".minic" / "logs" / "auto_ingest.jsonl"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def wait_for_auto_ingest(client: TestClient, timeout: float = 20.0) -> dict:
    """轮询 /rag/status 直到 last_auto_ingest_at 非空（后台任务完成）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get("/rag/status", headers=HEADERS).json()
        if data.get("last_auto_ingest_at"):
            return data
        time.sleep(0.05)
    raise AssertionError("自动入库未在超时时间内完成")


class SlowEmbeddingProvider(MockEmbeddingProvider):
    """每次 embedding 延迟 0.15s，用于验证启动不阻塞。"""

    def embed_texts(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        time.sleep(0.15)
        return super().embed_texts(texts, text_type)


# ---------------------------------------------------------------- 配置触发

def test_auto_ingest_on_startup(tmp_path: Path) -> None:
    """配置 auto_ingest_paths 后启动自动入库，文档可列出且状态字段非空。"""
    kb = make_kb(tmp_path)
    settings = make_settings(tmp_path, [str(kb)])
    with make_client(tmp_path, settings) as client:
        wait_for_auto_ingest(client)
        documents = client.get("/rag/documents", headers=HEADERS).json()["documents"]
        assert len(documents) == 3
        status = client.get("/rag/status", headers=HEADERS).json()
        assert status["last_auto_ingest_at"]
        # 每个路径一条结果日志
        log = read_auto_ingest_log(tmp_path)
        assert len(log) == 1
        assert log[0]["path"] == str(kb)
        assert log[0]["ingested"] == 3
        assert log[0]["skipped"] == 0
        assert log[0]["failed"] == []
        assert log[0]["error"] is None


def test_multiple_auto_ingest_paths(tmp_path: Path) -> None:
    """多路径配置全部入库，未配置路径不触发。"""
    kb_a = make_kb(tmp_path, "kb-a")
    kb_b = make_kb(tmp_path, "kb-b")
    settings = make_settings(tmp_path, [str(kb_a), str(kb_b)])
    with make_client(tmp_path, settings) as client:
        wait_for_auto_ingest(client)
        documents = client.get("/rag/documents", headers=HEADERS).json()["documents"]
        assert len(documents) == 6
        sources = {document["source"] for document in documents}
        assert len(sources) == 2  # 两个路径分别派生独立 source
        log = read_auto_ingest_log(tmp_path)
        assert len(log) == 2


def test_no_auto_ingest_when_not_configured(tmp_path: Path) -> None:
    """未配置 auto_ingest_paths（默认空）时启动不触发入库。"""
    kb = make_kb(tmp_path)
    settings = make_settings(tmp_path, [])
    with make_client(tmp_path, settings) as client:
        time.sleep(0.3)  # 留出窗口确认后台不会启动入库
        documents = client.get("/rag/documents", headers=HEADERS).json()["documents"]
        assert documents == []
        status = client.get("/rag/status", headers=HEADERS).json()
        assert status["last_auto_ingest_at"] is None
        assert read_auto_ingest_log(tmp_path) == []


# ---------------------------------------------------------------- 启动不阻塞

def test_startup_does_not_block_health(tmp_path: Path, monkeypatch) -> None:
    """启动后台执行自动入库：/health 立即返回，入库在后台完成。"""
    kb = make_kb(tmp_path)
    settings = make_settings(tmp_path, [str(kb)])
    monkeypatch.setattr("minic.rag.embeddings.create_embedding_provider", lambda s: SlowEmbeddingProvider(dimension=64))
    with make_client(tmp_path, settings) as client:
        start = time.monotonic()
        assert client.get("/health").status_code == 200  # startup 已返回，未等待入库
        assert time.monotonic() - start < 2.0
        wait_for_auto_ingest(client)
        documents = client.get("/rag/documents", headers=HEADERS).json()["documents"]
        assert len(documents) == 3


# ---------------------------------------------------------------- 增量幂等

def test_incremental_restart(tmp_path: Path) -> None:
    """再次启动未变化文件 skipped；修改文件重新入库；新增文件入库。"""
    kb = make_kb(tmp_path)
    settings = make_settings(tmp_path, [str(kb)])

    with make_client(tmp_path, settings) as client:  # 第一次启动：全部入库
        wait_for_auto_ingest(client)
        assert len(client.get("/rag/documents", headers=HEADERS).json()["documents"]) == 3

    with make_client(tmp_path, settings) as client:  # 重启：未变化全部 skipped
        wait_for_auto_ingest(client)
        latest = read_auto_ingest_log(tmp_path)[-1]
        assert latest["ingested"] == 0
        assert latest["skipped"] == 3

    (kb / "doc0.md").write_text(  # 修改一个文件
        "# 文档 0 已修改\n\n修改后的内容，包含唯一关键词：奇异果蜜柚。",
        encoding="utf-8",
    )
    with make_client(tmp_path, settings) as client:
        wait_for_auto_ingest(client)
        latest = read_auto_ingest_log(tmp_path)[-1]
        assert latest["ingested"] == 1
        assert latest["skipped"] == 2
        results = client.get(
            "/rag/query", headers=HEADERS, params={"q": "奇异果蜜柚", "top_k": 3}
        ).json()["results"]
        assert results  # 新内容可检索到

    (kb / "doc3.md").write_text("# 文档 3\n\n新增的文档。", encoding="utf-8")
    with make_client(tmp_path, settings) as client:
        wait_for_auto_ingest(client)
        latest = read_auto_ingest_log(tmp_path)[-1]
        assert latest["ingested"] == 1
        assert latest["skipped"] == 3
        documents = client.get("/rag/documents", headers=HEADERS).json()["documents"]
        assert len(documents) == 4


# ---------------------------------------------------------------- per-source 并发锁

def _concurrent_settings() -> AppSettings:
    return AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 64},
    )


def test_concurrent_ingest_same_source(tmp_path: Path) -> None:
    """同一 source 并发 ingest 互斥：只有一次实际入库，其余跳过，最终无丢文档/重复。"""
    kb = make_kb(tmp_path)
    rag = RagStore(
        rag_data_dir=tmp_path / "rag-data",
        settings=_concurrent_settings(),
        embedding_provider=MockEmbeddingProvider(dimension=64),
    )

    def run(_: int) -> tuple[int, int]:
        result = rag.ingest_directory(path=str(kb), extensions=[".md"], source="shared-source")
        return result.ingested, result.skipped

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(8)))

    assert sum(ingested for ingested, _ in results) == 3  # 锁保证只入库一次
    assert sum(skipped for _, skipped in results) == 8 * 3 - 3
    documents = rag.list_documents(source="shared-source")[0]
    assert len(documents) == 3
    doc_ids = [document["doc_id"] for document in documents]
    assert len(set(doc_ids)) == 3  # 无重复文档


def test_concurrent_ingest_different_sources(tmp_path: Path) -> None:
    """不同 source 使用独立锁，可并行且各自完整入库。"""
    kb_a = make_kb(tmp_path, "kb-a")
    kb_b = make_kb(tmp_path, "kb-b")
    rag = RagStore(
        rag_data_dir=tmp_path / "rag-data",
        settings=_concurrent_settings(),
        embedding_provider=MockEmbeddingProvider(dimension=64),
    )

    def run(args: tuple[str, str]) -> None:
        folder, source = args
        rag.ingest_directory(path=folder, extensions=[".md"], source=source)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, [(str(kb_a), "src-a"), (str(kb_b), "src-b")]))

    assert len(rag.list_documents(source="src-a")[0]) == 3
    assert len(rag.list_documents(source="src-b")[0]) == 3
    assert len(rag.list_documents()[0]) == 6


# ---------------------------------------------------------------- 失败容忍

def test_auto_ingest_failure_tolerance(tmp_path: Path) -> None:
    """无效路径 + 有效路径：启动正常，有效路径入库，无效路径记录错误不中断。"""
    kb = make_kb(tmp_path)
    missing = tmp_path / "not-exist"
    settings = make_settings(tmp_path, [str(missing), str(kb)])
    with make_client(tmp_path, settings) as client:
        assert client.get("/health").status_code == 200
        wait_for_auto_ingest(client)
        documents = client.get("/rag/documents", headers=HEADERS).json()["documents"]
        assert len(documents) == 3
        log = read_auto_ingest_log(tmp_path)
        assert len(log) == 2
        failed_entry = next(entry for entry in log if entry["path"] == str(missing))
        assert failed_entry["error"]  # 无效路径记录错误
        assert failed_entry["ingested"] == 0
        ok_entry = next(entry for entry in log if entry["path"] == str(kb))
        assert ok_entry["ingested"] == 3
        assert ok_entry["error"] is None
