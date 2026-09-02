"""G4 图包结构与公共 API 测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from langchain_core.messages import AIMessage, AIMessageChunk

from minic.chat.models import MockChatModel
from minic.core.config import AppSettings
from minic.graph import (
    ChatState,
    ToolSpec,
    build_chat_graph,
    build_super_graph,
    classify_scope,
    get_tool,
    list_tools,
    unwrap_json_answer,
)
from minic.graph.action.action_graph import build_action_graph
from minic.graph.knowledge.knowledge_graph import build_knowledge_graph
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


def test_public_api_imports() -> None:
    """公共符号可从 minic.graph 导入。"""
    assert callable(build_super_graph)
    assert callable(build_chat_graph)
    assert callable(classify_scope)
    assert callable(unwrap_json_answer)
    assert callable(get_tool)
    assert callable(list_tools)
    assert ChatState is not None
    assert ToolSpec is not None


def test_graph_builders_return_compiled_graphs(tmp_path: Path) -> None:
    """三个构建函数均可独立调用并返回编译后的图。"""
    settings, memory_store, rag_store = _env(tmp_path)
    model = MockChatModel()
    assert build_knowledge_graph(rag_store, model, memory_store, settings) is not None
    assert build_action_graph(rag_store, model, memory_store, settings) is not None
    assert build_super_graph(rag_store, model, memory_store, settings) is not None
    assert build_chat_graph(rag_store, model, memory_store, settings) is not None


def test_knowledge_behavior_keeps_sources(tmp_path: Path) -> None:
    """知识库子图仍返回带来源回答，且不写记忆。"""
    settings, memory_store, rag_store = _env(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "# MiniC 架构\n\n"
        "MiniC 使用 LangGraph 作为核心工作流引擎。\n\n"
        "## 检索\n\n"
        "MiniC 使用 Chroma 和 BM25 做混合检索。",
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
    assert final["sources"]
    assert final["sources"][0]["file_path"].endswith("guide.md")
    assert not (tmp_path / "global-memory" / "minic.md").exists()
    assert not (tmp_path / ".minic" / "memory" / "minic.md").exists()


def test_personal_info_writes_global_memory(tmp_path: Path) -> None:
    """个人信息通过新包写入全局记忆。"""
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


def test_project_info_writes_project_memory(tmp_path: Path) -> None:
    """项目约定通过新包写入项目记忆。"""
    settings, memory_store, rag_store = _env(tmp_path)
    model = FakeModel(
        intent="chat_action",
        topics=[{"topic": "项目约定", "content": "用 Python", "scope": "project"}],
    )
    graph = build_super_graph(rag_store, model, memory_store, settings)
    final = asyncio.run(
        _run_graph(
            graph,
            {
                "thread_id": "p1",
                "workspace": str(tmp_path),
                "user_message": "这个项目约定用 Python",
                "history": [],
            },
        )
    )
    assert final["answer"] == "好的，我记住了。"
    content = (tmp_path / ".minic" / "memory" / "minic.md").read_text(encoding="utf-8")
    assert "项目约定" in content
    assert "Python" in content
    assert not (tmp_path / "global-memory" / "minic.md").exists()


def test_memory_query_recalls_global_memory(tmp_path: Path) -> None:
    """新会话 memory_query 从全局记忆回忆个人信息。"""
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


def test_tool_registry() -> None:
    """工具注册表包含全部工具且 read/write 分类正确。"""
    tools = list_tools()
    names = {tool.name for tool in tools}
    assert names == {
        "Read",
        "ReadMemory",
        "TextSearch",
        "Lint",
        "Write",
        "Edit",
        "Format",
        "Bash",
        "GitStatus",
        "GitDiff",
        "GitLog",
        "GitCommit",
        "GitBranch",
        "DelegateToSubagent",
        "IngestDirectory",
    }
    assert get_tool("Read").category == "read"
    assert get_tool("Write").category == "write"
    assert get_tool("Edit").category == "write"
    assert get_tool("Bash").category == "exec"
    assert get_tool("DelegateToSubagent").category == "exec"
    assert get_tool("GitBranch").category == "write"
    assert get_tool("Unknown") is None
    assert all(isinstance(tool, ToolSpec) for tool in tools)


def test_classify_scope_and_unwrap() -> None:
    """作用域分类与 JSON 解包保持原行为。"""
    assert classify_scope("姓名", "张三") == "global"
    assert classify_scope("项目约定", "用 Python") == "project"
    assert unwrap_json_answer('```json\n{"answer": "纯文本"}\n```') == "纯文本"
