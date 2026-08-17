"""M2 总图 + 子图架构测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from langchain_core.messages import AIMessage, AIMessageChunk

from minic.graph import build_super_graph
from minic.chat.models import MockChatModel
from minic.core.config import AppSettings
from minic.memory import LongTermMemoryStore
from minic.rag.embeddings import MockEmbeddingProvider
from minic.rag.store import RagStore


class FakeModel:
    """测试用模型，可配置总图意图、记忆提取结果和回答。"""

    def __init__(
        self,
        intent: str = "knowledge",
        topics: list[dict[str, str]] | None = None,
        memory_answer: str = "你叫李四。",
    ) -> None:
        self.intent = intent
        self.topics = topics or []
        self.memory_answer = memory_answer

    async def ainvoke(self, messages: list) -> AIMessage:
        """按系统提示返回固定 JSON。"""
        system = messages[0].content
        if "意图路由" in system:
            return AIMessage(content=json.dumps({"intent": self.intent}))
        if "记忆提取" in system:
            return AIMessage(content=json.dumps(self.topics))
        return AIMessage(content="ok")

    async def astream(self, messages: list):
        """按系统提示流式返回固定文本。"""
        system = messages[0].content
        if "用户刚刚提供了个人信息" in system:
            parts = ["好的，我记住了。"]
        elif "长期记忆" in system and "关于自己的问题" in system:
            parts = [self.memory_answer]
        elif "未检索到" in system:
            parts = ["未检索到。"]
        else:
            parts = ["这是基于资料生成的模拟回答。"]
        for part in parts:
            yield AIMessageChunk(content=part)


def _env(tmp_path: Path):
    """构造隔离的 RAG 与记忆环境。"""
    settings = AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 64},
    )
    memory_store = LongTermMemoryStore(tmp_path / "global-memory", tmp_path)
    rag_store = RagStore(tmp_path / "rag-data", settings, MockEmbeddingProvider(64))
    return settings, memory_store, rag_store


async def _run_graph(graph, state: dict) -> dict:
    """运行图并返回最终状态。"""
    final = {}
    config = {"configurable": {"thread_id": str(state.get("run_id") or state.get("thread_id") or "test")}}
    async for mode, payload in graph.astream(state, config=config, stream_mode=["values"]):
        if mode == "values":
            final = payload
    return final


def test_inventory_returns_document_list(tmp_path: Path) -> None:
    """RagStore.inventory 从 metadata.json 返回文档清单。"""
    settings, _, rag_store = _env(tmp_path)
    assert rag_store.inventory() == []

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "# 标题\n\nMiniC 使用 LangGraph 作为核心工作流引擎。",
        encoding="utf-8",
    )
    result = rag_store.ingest_directory(str(docs))
    assert result.ingested == 1

    items = rag_store.inventory()
    assert len(items) == 1
    item = items[0]
    assert item["file_path"] == "guide.md"
    assert item["chunk_count"] > 0
    assert item["embedded_at"]
    assert item["source"]


def test_super_graph_knowledge_uses_rag_with_sources(tmp_path: Path) -> None:
    """知识问题走知识库子图，返回带来源的回答且不写长期记忆。"""
    settings, memory_store, rag_store = _env(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "# MiniC 架构\n\n"
        "MiniC 使用 LangGraph 作为核心工作流引擎。\n\n"
        "## 检索\n\n"
        "MiniC 使用 Chroma 和 BM25 做混合检索，回答会带文件路径和章节来源。",
        encoding="utf-8",
    )
    rag_store.ingest_directory(str(docs))
    graph = build_super_graph(rag_store, MockChatModel(), memory_store, settings)
    final = asyncio.run(
        _run_graph(
            graph,
            {
                "thread_id": "q1",
                "workspace": str(tmp_path),
                "user_message": "LangGraph 的作用是什么",
                "history": [],
            },
        )
    )
    assert final["answer"] == "这是基于资料生成的模拟回答。"
    assert final["sources"]
    assert final["sources"][0]["file_path"].endswith("guide.md")
    assert not (tmp_path / "global-memory" / "minic.md").exists()


def test_super_graph_empty_knowledge_base_returns_not_found(tmp_path: Path) -> None:
    """知识库为空时总图路由仍正常，回答保持未检索到。"""
    settings, memory_store, rag_store = _env(tmp_path)
    graph = build_super_graph(rag_store, FakeModel(intent="knowledge"), memory_store, settings)
    final = asyncio.run(
        _run_graph(
            graph,
            {
                "thread_id": "q2",
                "workspace": str(tmp_path),
                "user_message": "量子菠萝星球的定义是什么",
                "history": [],
            },
        )
    )
    assert "未检索到。" in final["answer"]
    assert final["sources"] == []


def test_super_graph_knowledge_never_runs_memory_extraction(tmp_path: Path) -> None:
    """知识问题即使模型能提取个人信息，也不得写入长期记忆。"""
    settings, memory_store, rag_store = _env(tmp_path)
    model = FakeModel(intent="knowledge", topics=[{"topic": "姓名", "content": "张三"}])
    graph = build_super_graph(rag_store, model, memory_store, settings)
    final = asyncio.run(
        _run_graph(
            graph,
            {
                "thread_id": "q3",
                "workspace": str(tmp_path),
                "user_message": "LangGraph 是什么",
                "history": [],
            },
        )
    )
    assert "未检索到。" in final["answer"]
    assert not (tmp_path / "global-memory" / "minic.md").exists()


def test_super_graph_chat_action_writes_memory_and_confirms(tmp_path: Path) -> None:
    """用户信息陈述走动作子图，写入长期记忆并确认回复。"""
    settings, memory_store, rag_store = _env(tmp_path)
    model = FakeModel(intent="chat_action", topics=[{"topic": "姓名", "content": "李四"}])
    graph = build_super_graph(rag_store, model, memory_store, settings)
    final = asyncio.run(
        _run_graph(
            graph,
            {
                "thread_id": "t1",
                "workspace": str(tmp_path),
                "user_message": "我叫李四",
                "history": [],
            },
        )
    )
    assert final["answer"] == "好的，我记住了。"
    content = (tmp_path / "global-memory" / "minic.md").read_text(encoding="utf-8")
    assert "姓名" in content
    assert "李四" in content
    assert not (tmp_path / ".minic" / "memory" / "minic.md").exists()


def test_super_graph_memory_query_uses_memory_not_rag(tmp_path: Path) -> None:
    """memory_query 走动作子图，使用长期记忆回答，不依赖 RAG。"""
    settings, memory_store, rag_store = _env(tmp_path)
    write_model = FakeModel(intent="chat_action", topics=[{"topic": "姓名", "content": "李四"}])
    write_graph = build_super_graph(rag_store, write_model, memory_store, settings)
    asyncio.run(
        _run_graph(
            write_graph,
            {
                "thread_id": "write",
                "workspace": str(tmp_path),
                "user_message": "我叫李四",
                "history": [],
            },
        )
    )

    query_model = FakeModel(intent="memory_query", memory_answer="你叫李四。")
    query_graph = build_super_graph(rag_store, query_model, memory_store, settings)
    final = asyncio.run(
        _run_graph(
            query_graph,
            {
                "thread_id": "read",
                "workspace": str(tmp_path),
                "user_message": "我叫什么",
                "history": [],
            },
        )
    )
    assert "李四" in final["answer"]
