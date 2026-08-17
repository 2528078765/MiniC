"""M1 对话意图路由与通用记忆提取测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from langchain_core.messages import AIMessage, AIMessageChunk

from minic.graph import build_super_graph, classify_scope
from minic.core.config import AppSettings
from minic.memory import LongTermMemoryStore
from minic.rag.embeddings import MockEmbeddingProvider
from minic.rag.store import RagStore


class FakeChatModel:
    """M1 测试用模型，返回可配置的意图、提取结果和回答。"""

    def __init__(
        self,
        intent: str = "knowledge",
        topics: list[dict[str, str]] | None = None,
        memory_answer: str = "你叫张三。",
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


def test_user_info_written_to_global_memory(tmp_path: Path) -> None:
    """用户陈述个人信息时写入全局长期记忆并得到确认。"""
    settings, memory_store, rag_store = _env(tmp_path)
    model = FakeChatModel(intent="chat_action", topics=[{"topic": "姓名", "content": "张三"}])
    graph = build_super_graph(rag_store, model, memory_store, settings)
    final = asyncio.run(
        _run_graph(
            graph,
            {
                "thread_id": "t1",
                "workspace": str(tmp_path),
                "user_message": "我叫张三",
                "history": [],
            },
        )
    )
    assert final["answer"] == "好的，我记住了。"
    content = (tmp_path / "global-memory" / "minic.md").read_text(encoding="utf-8")
    assert "姓名" in content
    assert "张三" in content
    assert not (tmp_path / ".minic" / "memory" / "minic.md").exists()


def test_general_personal_info_extraction(tmp_path: Path) -> None:
    """多个不同姓名、偏好、身份样例均可通用识别写入。"""
    examples = [
        ("我叫李四", [{"topic": "姓名", "content": "李四"}]),
        ("我喜欢用Python", [{"topic": "编程偏好", "content": "Python"}]),
        ("我是产品经理", [{"topic": "职业", "content": "产品经理"}]),
    ]
    for index, (message, topics) in enumerate(examples):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        settings, memory_store, rag_store = _env(case_dir)
        model = FakeChatModel(intent="chat_action", topics=topics)
        graph = build_super_graph(rag_store, model, memory_store, settings)
        asyncio.run(
            _run_graph(
                graph,
                {
                    "thread_id": f"t{index}",
                    "workspace": str(case_dir),
                    "user_message": message,
                    "history": [],
                },
            )
        )
        content = (case_dir / "global-memory" / "minic.md").read_text(encoding="utf-8")
        assert topics[0]["topic"] in content
        assert topics[0]["content"] in content
        assert not (case_dir / ".minic" / "memory" / "minic.md").exists()


def test_memory_query_uses_long_term_memory(tmp_path: Path) -> None:
    """新会话追问用户信息时使用长期记忆回答，不依赖 RAG。"""
    settings, memory_store, rag_store = _env(tmp_path)
    write_model = FakeChatModel(intent="chat_action", topics=[{"topic": "姓名", "content": "张三"}])
    graph = build_super_graph(rag_store, write_model, memory_store, settings)
    asyncio.run(
        _run_graph(
            graph,
            {
                "thread_id": "write",
                "workspace": str(tmp_path),
                "user_message": "我叫张三",
                "history": [],
            },
        )
    )

    query_model = FakeChatModel(intent="memory_query", memory_answer="你叫张三。")
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
    assert "张三" in final["answer"]


def test_knowledge_question_still_uses_rag(tmp_path: Path) -> None:
    """普通知识问题仍走 RAG，无结果时保持未检索到。"""
    settings, memory_store, rag_store = _env(tmp_path)
    model = FakeChatModel(intent="knowledge")
    graph = build_super_graph(rag_store, model, memory_store, settings)
    final = asyncio.run(
        _run_graph(
            graph,
            {
                "thread_id": "q1",
                "workspace": str(tmp_path),
                "user_message": "量子菠萝星球的定义是什么",
                "history": [],
            },
        )
    )
    assert "未检索到。" in final["answer"]
    assert not (tmp_path / "global-memory" / "minic.md").exists()
    assert not (tmp_path / ".minic" / "memory" / "minic.md").exists()


def test_memory_writes_global_when_workspace_unwritable(tmp_path: Path) -> None:
    """不可写工作区下，个人信息仍写入全局记忆且不报错。"""
    settings, memory_store, rag_store = _env(tmp_path)
    workspace = tmp_path / "unwritable-workspace"
    workspace.mkdir()
    core_root = tmp_path / "core-root"
    core_root.mkdir()
    model = FakeChatModel(intent="chat_action", topics=[{"topic": "姓名", "content": "王五"}])
    graph = build_super_graph(rag_store, model, memory_store, settings)
    final = asyncio.run(
        _run_graph(
            graph,
            {
                "thread_id": "t1",
                "workspace": str(workspace),
                "project_root": str(core_root),
                "user_message": "我叫王五",
                "history": [],
            },
        )
    )
    assert final["answer"] == "好的，我记住了。"
    content = (tmp_path / "global-memory" / "minic.md").read_text(encoding="utf-8")
    assert "姓名" in content
    assert "王五" in content
    assert not (workspace / ".minic" / "memory" / "minic.md").exists()
    assert not (core_root / ".minic" / "memory" / "minic.md").exists()


def test_project_info_written_to_project_memory(tmp_path: Path) -> None:
    """项目约定类信息写入项目长期记忆，不写全局。"""
    settings, memory_store, rag_store = _env(tmp_path)
    model = FakeChatModel(
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


def test_migrate_project_to_global_is_generic_and_idempotent(tmp_path: Path) -> None:
    """通用迁移只把 global 分类主题从项目记忆移到全局，重复执行无新增。"""
    settings, memory_store, rag_store = _env(tmp_path)
    memory_store.add_topic("project", str(tmp_path), "姓名", "张三", source="user")
    memory_store.add_topic("project", str(tmp_path), "项目约定", "用 Python", source="user")

    first = memory_store.migrate_project_to_global(classify_scope, str(tmp_path))
    assert [item["topic"] for item in first] == ["姓名"]
    global_content = (tmp_path / "global-memory" / "minic.md").read_text(encoding="utf-8")
    project_content = (tmp_path / ".minic" / "memory" / "minic.md").read_text(encoding="utf-8")
    assert "姓名" in global_content
    assert "张三" in global_content
    assert "项目约定" in project_content
    assert "姓名" not in project_content

    second = memory_store.migrate_project_to_global(classify_scope, str(tmp_path))
    assert second == []


def test_memory_env_isolated_to_tmp(tmp_path: Path) -> None:
    """测试记忆环境只使用临时目录，防止污染真实用户/项目数据。"""
    settings, memory_store, rag_store = _env(tmp_path)
    assert Path(memory_store.global_dir).is_relative_to(tmp_path)
    assert Path(memory_store.project_root).is_relative_to(tmp_path)
