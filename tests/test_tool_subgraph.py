"""G5 工具调用子图测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from langchain_core.callbacks.manager import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from minic.chat.engine import ChatEngine
from minic.chat.memory import ShortMemoryStore
from minic.core.config import AppSettings
from minic.graph import build_super_graph
from minic.memory import LongTermMemoryStore
from minic.rag.embeddings import MockEmbeddingProvider
from minic.rag.store import RagStore
from minic.recovery import ToolExecutionLog
from minic.tools.runtime import ToolRuntime
from minic.tools.service import ApprovalManager, PermissionStore, ToolExecutor


class FakeToolModel(BaseChatModel):
    """测试用模型：先返回工具调用（原生 tool_calls），再返回最终回答。"""

    model_name: str = "mock-tool"
    temperature: float = 0.0
    decisions: list[dict] = Field(default_factory=list)
    captured: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        """返回模型类型标识。"""
        return "mock-tool"

    def bind_tools(self, tools: list, **kwargs) -> "FakeToolModel":
        """忽略工具绑定（mock 无原生 tools 参数）。"""
        del tools, kwargs
        return self

    def _decide(self, messages: list[BaseMessage]) -> tuple[str, str, dict | None]:
        """按系统提示返回 (文本内容, tool_calls) 决策。"""
        self.captured.append(list(messages))  # 记录模型实际收到的消息序列
        system = messages[0].content
        if "意图路由" in system:
            return json.dumps({"intent": "chat_action"}), None
        if "记忆提取" in system:
            return "[]", None
        if "可用工具" in system:  # agent 节点提示
            if self.decisions:
                decision = self.decisions.pop(0)
                tool = decision.get("tool")
                if tool:
                    call = {
                        "name": tool,
                        "args": decision.get("args") or {},
                        "id": f"call_{len(self.captured)}",
                    }
                    return "", call
                return "最终回答。", None
            return "最终回答。", None
        return "ok", None

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs,
    ) -> ChatResult:
        """同步生成完整回答。"""
        del stop, run_manager, kwargs
        content, call = self._decide(messages)
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content=content, tool_calls=[call] if call else [])
                )
            ]
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs,
    ) -> ChatResult:
        """异步生成完整回答。"""
        return self._generate(messages, stop=stop, **kwargs)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs,
    ):
        """同步按分片流式返回回答。"""
        del stop, run_manager, kwargs
        content, call = self._decide(messages)
        if call is not None:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_calls=[call]))
            return
        for i in range(0, len(content), 4):
            yield ChatGenerationChunk(message=AIMessageChunk(content=content[i : i + 4]))

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs,
    ):
        """异步按分片流式返回回答。"""
        del stop, run_manager, kwargs
        content, call = self._decide(messages)
        if call is not None:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_calls=[call]))
            return
        for i in range(0, len(content), 4):
            yield ChatGenerationChunk(message=AIMessageChunk(content=content[i : i + 4]))


def _env(tmp_path: Path, decisions: list[dict]):
    """构造隔离的 RAG、记忆、工具与审批环境。"""
    settings = AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 64},
    )
    memory_store = LongTermMemoryStore(tmp_path / "global-memory", tmp_path)
    rag_store = RagStore(tmp_path / "rag-data", settings, MockEmbeddingProvider(64))
    permission_store = PermissionStore(
        tmp_path / "global-permissions.json",
        tmp_path / ".minic" / "permissions.json",
    )
    tool_executor = ToolExecutor(
        tmp_path,
        tmp_path / ".minic" / "backups" / "files",
        memory_store,
    )
    approval_manager = ApprovalManager(permission_store, tmp_path, settings)
    tool_log = ToolExecutionLog(tmp_path / ".minic" / "logs" / "tool_execution.jsonl")
    tool_runtime = ToolRuntime(approval_manager, tool_executor, tool_log)
    chat_model = FakeToolModel(decisions=decisions)
    graph = build_super_graph(
        rag_store=rag_store,
        chat_model=chat_model,
        memory_store=memory_store,
        settings=settings,
        tool_runtime=tool_runtime,
    )
    return {
        "graph": graph,
        "runtime": tool_runtime,
        "approval": approval_manager,
        "permissions": permission_store,
        "root": tmp_path,
        "log": tool_log,
        "memory": memory_store,
        "settings": settings,
        "model": chat_model,
    }


def _state(tmp_path: Path, thread_id: str = "t1") -> dict:
    """构造图初始状态。"""
    return {
        "thread_id": thread_id,
        "workspace": str(tmp_path),
        "project_root": str(tmp_path),
        "user_message": "帮我读取文件",
        "history": [],
        "run_id": "run-1",
        "message_id": "msg-1",
    }


async def _run_graph(
    graph,
    state: dict,
    runtime: ToolRuntime | None = None,
    thread_id: str = "t1",
) -> dict:
    """运行图并返回最终状态。"""
    events: list[tuple[str, dict]] = []
    if runtime is not None:
        runtime.attach(thread_id, lambda name, data: events.append((name, data)))
    try:
        final = {}
        config = {"configurable": {"thread_id": str(state.get("run_id") or state.get("thread_id") or "test")}}
        async for mode, payload in graph.astream(state, config=config, stream_mode=["values"]):
            if mode == "values":
                final = payload
        return final
    finally:
        if runtime is not None:
            runtime.detach(thread_id)


async def _run_with_events(
    graph,
    state: dict,
    runtime: ToolRuntime,
    thread_id: str,
    decision: str | None = None,
):
    """并发消费工具事件，审批时自动提交。"""
    queue: asyncio.Queue = asyncio.Queue()
    runtime.attach(thread_id, lambda name, data: queue.put_nowait((name, data)))
    events: list[tuple[str, dict]] = []
    final: dict = {}

    async def collect() -> None:
        nonlocal final
        config = {"configurable": {"thread_id": str(state.get("run_id") or state.get("thread_id") or "test")}}
        async for mode, payload in graph.astream(state, config=config, stream_mode=["values"]):
            if mode == "values":
                final = payload

    task = asyncio.create_task(collect())
    while not task.done():
        try:
            name, data = await asyncio.wait_for(queue.get(), timeout=5)
        except asyncio.TimeoutError:
            break
        events.append((name, data))
        if name == "approval_requested" and decision is not None:
            runtime.approval_manager.submit(thread_id, data["id"], decision)
    await task
    while not queue.empty():
        events.append(queue.get_nowait())
    runtime.detach(thread_id)
    return final, events


def test_read_tool_auto_executes_and_result_enters_answer(tmp_path: Path) -> None:
    """读工具自动执行，结果进入回答上下文。"""
    target = tmp_path / "a.txt"
    target.write_text("hello tool", encoding="utf-8")
    env = _env(tmp_path, [{"tool": "Read", "args": {"path": str(target)}}, {"tool": None}])
    final = asyncio.run(_run_graph(env["graph"], _state(tmp_path), env["runtime"]))

    assert final["tool_context"][0]["tool"] == "Read"
    assert final["tool_context"][0]["status"] == "completed"
    assert final["tool_context"][0]["output"] == "hello tool"
    assert final["answer"] == "最终回答。"
    records = env["log"].read()
    assert any(record["event"] == "intent" for record in records)
    assert any(record["event"] == "result" for record in records)


def test_write_tool_approval_allow_once(tmp_path: Path) -> None:
    """Write 触发审批，allow_once 后执行并带备份。"""
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    env = _env(
        tmp_path,
        [{"tool": "Write", "args": {"path": str(target), "content": "new"}}, {"tool": None}],
    )
    final, events = asyncio.run(
        _run_with_events(env["graph"], _state(tmp_path), env["runtime"], "t1", "allow_once")
    )

    names = [name for name, _ in events]
    assert "approval_requested" in names
    assert "approval_result" in names
    assert "tool_result" in names
    assert final["tool_context"][0]["status"] == "completed"
    assert target.read_text(encoding="utf-8") == "new"
    assert final["tool_context"][0]["backup_id"]


def test_write_tool_deny_prevents_execution(tmp_path: Path) -> None:
    """deny 后工具不执行，模型继续回答。"""
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    env = _env(
        tmp_path,
        [{"tool": "Write", "args": {"path": str(target), "content": "new"}}, {"tool": None}],
    )
    final, events = asyncio.run(
        _run_with_events(env["graph"], _state(tmp_path), env["runtime"], "t1", "deny")
    )

    names = [name for name, _ in events]
    assert "approval_requested" in names
    assert "approval_result" in names
    assert final["tool_context"][0]["status"] == "denied"
    assert target.read_text(encoding="utf-8") == "old"
    assert final["answer"] == "最终回答。"


def test_tool_loop_stops_after_eight_calls(tmp_path: Path) -> None:
    """模型持续调用工具时最多循环 8 次。"""
    target = tmp_path / "a.txt"
    target.write_text("data", encoding="utf-8")
    decisions = [{"tool": "Read", "args": {"path": str(target)}}] * 20
    env = _env(tmp_path, decisions)
    final = asyncio.run(_run_graph(env["graph"], _state(tmp_path), env["runtime"]))
    assert len(final["tool_context"]) == 8
    assert final["tool_loop_count"] == 8
    assert final["answer"]  # 8 轮上限后仍有回答（兜底或提取）


def test_chat_engine_streams_tool_events_in_order(tmp_path: Path) -> None:
    """ChatEngine 实时转发工具事件，顺序符合 SSE 契约。"""
    target = tmp_path / "a.txt"
    target.write_text("data", encoding="utf-8")
    env = _env(tmp_path, [{"tool": "Read", "args": {"path": str(target)}}, {"tool": None}])
    short_memory = ShortMemoryStore(tmp_path / "memory" / "short_memory")
    thread_id = "chat-1"
    short_memory.create(thread_id, str(tmp_path))
    engine = ChatEngine(
        rag_store=None,
        short_memory=short_memory,
        graph=env["graph"],
        tool_runtime=env["runtime"],
    )

    async def collect() -> list[tuple[str, dict]]:
        events = []
        async for event_name, data in engine.stream_chat(
            thread_id=thread_id,
            workspace=str(tmp_path),
            project_root=str(tmp_path),
            message="帮我读取文件",
            run_id="run-1",
            message_id="msg-1",
        ):
            events.append((event_name, data))
        return events

    events = asyncio.run(collect())
    names = [name for name, _ in events]
    assert names[:2] == ["message_start", "tool_call"]
    assert "tool_result" in names
    token_index = names.index("token")
    assert token_index > names.index("tool_result")
    assert names[-2:] == ["message_end", "done"]
    thread = short_memory.load(thread_id)
    assert thread["messages"][-1]["role"] == "ai"


def test_model_receives_tool_results_as_text_across_rounds(tmp_path: Path) -> None:
    """守护测试：多轮工具调用时工具结果以文本拼进 Human 消息（纯文本协议）。

    不传 AIMessage(tool_calls)/ToolMessage：DeepSeek thinking 模式对含 tool_calls
    的 assistant 消息要求 reasoning_content 原样回传，重构消息无法满足会 400。
    """
    target = tmp_path / "a.txt"
    target.write_text("data", encoding="utf-8")
    env = _env(
        tmp_path,
        [
            {"tool": "Read", "args": {"path": str(target)}},
            {"tool": "Read", "args": {"path": str(target)}},
            {"tool": None},
        ],
    )
    final = asyncio.run(_run_graph(env["graph"], _state(tmp_path), env["runtime"]))
    assert len(final["tool_context"]) == 2
    model = env["model"]
    assert model.captured, "模型应被调用过"
    # 教科书 ReAct：工具结果以原生 ToolMessage 回传 agent
    tool_messages = [
        message
        for call_messages in model.captured
        for message in call_messages
        if isinstance(message, ToolMessage)
    ]
    assert tool_messages, "后续决策轮应收到 ToolMessage"
    assert any("data" in str(message.content) for message in tool_messages)

