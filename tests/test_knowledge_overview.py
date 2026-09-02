"""知识库概况类问题（"你都知道什么"）测试：不检索、直接回答文档清单。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest import mock

from langchain_core.messages import AIMessage

from minic.graph.common import is_knowledge_overview
from minic.graph.knowledge.knowledge_node import knowledge_answer_node, retrieve_node
from minic.graph.super_node import route_node


class _RouteModel:
    """总是判 chat_action 的模型替身（验证硬兜底）。"""

    def __init__(self) -> None:
        self.invoke_prompts: list[str] = []

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.invoke_prompts.append(messages[0].content)
        return AIMessage(content=json.dumps({"intent": "chat_action"}))


class _AnswerModel:
    """记录回答提示词的模型替身。"""

    def __init__(self) -> None:
        self.stream_prompts: list[str] = []

    async def astream(self, messages: list[Any]):
        self.stream_prompts.append(messages[0].content)
        yield AIMessage(content="清单回答")


def _state(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    """构造图状态。"""
    state = {
        "thread_id": "t1",
        "workspace": str(tmp_path),
        "project_root": str(tmp_path),
        "user_message": "你都知道什么",
        "rewritten_query": "你都知道什么",
        "history": [],
        "intent": "knowledge",
    }
    state.update(extra)
    return state


def test_is_knowledge_overview_patterns() -> None:
    """概况类问法命中，普通知识问题不命中。"""
    assert is_knowledge_overview("你都知道什么")
    assert is_knowledge_overview("你知道些什么吗")
    assert is_knowledge_overview("知识库里有哪些文档")
    assert not is_knowledge_overview("什么是 LangGraph")
    assert not is_knowledge_overview("帮我写个函数")


def test_route_node_forces_knowledge_for_overview(tmp_path: Path) -> None:
    """模型把"你都知道什么"判成 chat_action 时硬兜底为 knowledge。"""
    model = _RouteModel()
    result = asyncio.run(
        route_node(_state(tmp_path, user_message="你都知道什么"), model, None, None, None, None)
    )
    assert result["intent"] == "knowledge"
    assert "你都知道什么" in model.invoke_prompts[0]


def test_retrieve_node_skips_search_for_overview(tmp_path: Path) -> None:
    """概况类问题不调用 RAG 检索。"""
    store = mock.Mock()
    result = asyncio.run(retrieve_node(_state(tmp_path), store))
    assert result == {"contexts": [], "sources": []}
    store.query.assert_not_called()


def test_knowledge_answer_node_lists_documents(tmp_path: Path) -> None:
    """概况类无检索结果时，提示词注入文档清单。"""
    model = _AnswerModel()
    state = _state(
        tmp_path,
        documents=[
            {"file_path": "a.md", "chunk_count": 3},
            {"file_path": "b.md", "chunk_count": 5},
        ],
        contexts=[],
    )
    result = asyncio.run(knowledge_answer_node(state, model, None, None, None))
    assert result["answer"] == "清单回答"
    prompt = model.stream_prompts[0]
    assert "a.md" in prompt
    assert "b.md" in prompt
    assert "未检索到" not in prompt


def test_knowledge_answer_node_empty_store(tmp_path: Path) -> None:
    """概况类且库为空时明确告知暂无文档。"""
    model = _AnswerModel()
    result = asyncio.run(
        knowledge_answer_node(_state(tmp_path, documents=[], contexts=[]), model, None, None, None)
    )
    assert result["answer"] == "清单回答"
    assert "暂无文档" in model.stream_prompts[0]


def test_knowledge_answer_node_retrieval_miss_unchanged(tmp_path: Path) -> None:
    """非概况类检索无结果仍回复"未检索到"。"""
    model = _AnswerModel()
    state = _state(tmp_path, user_message="什么是量子纠缠", rewritten_query="什么是量子纠缠", contexts=[])
    asyncio.run(knowledge_answer_node(state, model, None, None, None))
    assert "未检索到" in model.stream_prompts[0]


def test_knowledge_answer_node_contexts_include_documents(tmp_path: Path) -> None:
    """有检索资料时提示词也常驻文档清单（改述问法可获清单）。"""
    model = _AnswerModel()
    state = _state(
        tmp_path,
        user_message="你了解哪些知识",
        contexts=[{"file_path": "a.md", "section": "sec", "text": "无关资料"}],
        documents=[{"file_path": "a.md", "chunk_count": 3}],
    )
    asyncio.run(knowledge_answer_node(state, model, None, None, None))
    prompt = model.stream_prompts[0]
    assert "知识库文档清单" in prompt
    assert "a.md" in prompt
    assert "你都知道什么" in prompt  # 概况例外指示


def test_action_answer_node_injects_documents(tmp_path: Path) -> None:
    """chat_action 普通聊天提示词常驻文档清单（改述问法不依赖路由判断）。"""
    from minic.graph.action.action_node import action_answer_node

    model = _AnswerModel()
    state = _state(
        tmp_path,
        user_message="你了解哪些知识",
        intent="chat_action",
        documents=[{"file_path": "a.md", "chunk_count": 3}],
    )
    asyncio.run(action_answer_node(state, model, None, None, None))
    prompt = model.stream_prompts[0]
    assert "知识库文档清单" in prompt
    assert "a.md" in prompt


def test_append_documents_block_empty_store() -> None:
    """空清单不追加块。"""
    from minic.graph.common import append_documents_block

    assert append_documents_block("hello", []) == "hello"
    result = append_documents_block("hello", [{"file_path": "a.md", "chunk_count": 1}])
    assert "知识库文档清单" in result
    assert "a.md" in result
