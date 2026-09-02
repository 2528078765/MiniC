"""pytest 共享夹具。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minic.core.config import AppSettings
from minic.core.server import create_app


@pytest.fixture
def settings() -> AppSettings:
    """使用 mock 模型与 mock embedding 的测试配置。"""
    return AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 64},
        rag={
            "chunk_size": 500,
            "chunk_overlap": 100,
            "top_k": 5,
            "bm25_weight": 0.45,
            "vector_weight": 0.55,
        },
    )


@pytest.fixture
def temp_dirs(tmp_path: Path) -> dict[str, Path]:
    """临时 RAG 数据与短期记忆目录（双库隔离：项目库 + 全局库）。"""
    return {
        "rag_data": tmp_path / "rag-data",
        "global_rag_data": tmp_path / "global-rag-data",
        "short_memory": tmp_path / "memory" / "short_memory",
        "skills_global": tmp_path / "home" / ".minic" / "skills",
        "skills_project": tmp_path / ".minic" / "skills",
    }


@pytest.fixture
def client(settings: AppSettings, temp_dirs: dict[str, Path]) -> TestClient:
    """带测试令牌的应用客户端（双库隔离，入库默认路由到全局库）。"""
    app = create_app(
        settings=settings,
        token="test-token",
        project_root=Path.cwd(),
        rag_data_dir=temp_dirs["rag_data"],
        short_memory_dir=temp_dirs["short_memory"],
        skills_global_dir=temp_dirs["skills_global"],
        skills_project_dir=temp_dirs["skills_project"],
    )
    return TestClient(app)


@pytest.fixture
def markdown_dir(tmp_path: Path) -> Path:
    """包含可检索章节的 Markdown 文件夹。"""
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "guide.md").write_text(
        "# MiniC 架构\n\n"
        "MiniC 使用 LangGraph 作为核心工作流引擎。\n\n"
        "## 检索\n\n"
        "MiniC 使用 Chroma 和 BM25 做混合检索，回答会带文件路径和章节来源。",
        encoding="utf-8",
    )
    return folder
