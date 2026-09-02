"""G7c 子 Agent（基础版）测试。

覆盖：POST/GET /agents 接口、子任务与主会话隔离（short_memory 不写入）、
子任务内工具审批带 subagent_id、并发限制、超时、长期记忆只读注入、
DelegateToSubagent 注册与 tool_loop 转发、404。
全部使用 tmp_path 隔离与 mock 模型，不触碰真实数据目录。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

from fastapi.testclient import TestClient
from langchain_core.callbacks.manager import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from minic.core.config import AppSettings
from minic.core.server import create_app
from minic.graph import build_super_graph, get_tool
from minic.memory import LongTermMemoryStore
from minic.rag.embeddings import MockEmbeddingProvider
from minic.rag.store import RagStore
from minic.recovery import ToolExecutionLog
from minic.subagent import SubAgentManager
from minic.tools.runtime import ToolRuntime
from minic.tools.service import ApprovalManager, PermissionStore, ToolExecutor

AUTH_HEADERS = {"Authorization": "Bearer test-token"}


class FakeModel(BaseChatModel):


    def bind_tools(self, tools, **kwargs):
        """忽略工具绑定（mock）。"""
        del tools, kwargs
        return self
    """测试模型：子 Agent 提示输出 answer，工具提示输出 main_decisions。"""

    model_name: str = "mock-subagent"
    temperature: float = 0.0
    main_decisions: list[dict] = Field(default_factory=list)
    sub_decisions: list[dict] = Field(default_factory=list)
    sub_answer: str = "子任务完成"
    sleep_seconds: float = 0.0
    on_invoke: Callable[[], Any] | None = None
    captured: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        """返回模型类型标识。"""
        return "mock-subagent"

    def _decide(self, messages: list[BaseMessage]) -> tuple[str, dict | None]:
        """按系统提示返回 (文本内容, tool_calls) 决策并记录消息序列。"""
        self.captured.append(list(messages))
        system = messages[0].content if messages and isinstance(messages[0].content, str) else ""
        if "工具调用器" in system:  # 主会话 agent（工具列表含 DelegateToSubagent 描述，需先于子 Agent 判断）
            if self.main_decisions:
                decision = self.main_decisions.pop(0)
                tool = decision.get("tool")
                if tool:
                    call = {"name": tool, "args": decision.get("args") or {}, "id": f"call_{len(self.captured)}"}
                    return "", call
            return "最终回答。", None
        if "子 Agent" in system:
            if self.sub_decisions:
                return json.dumps(self.sub_decisions.pop(0)), None
            return json.dumps({"answer": self.sub_answer}), None
        if "意图路由" in system or "总图路由" in system:
            return json.dumps({"intent": "chat_action"}), None
        if "记忆提取" in system:
            return "[]", None
        return "ok", None

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
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
        **kwargs: Any,
    ) -> ChatResult:
        """异步生成完整回答（支持可观测回调与睡眠）。"""
        if self.on_invoke is not None:
            await self.on_invoke()
        if self.sleep_seconds:
            await asyncio.sleep(self.sleep_seconds)
        return self._generate(messages, stop=stop, **kwargs)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ):
        """同步按分片流式返回回答。"""
        del stop, kwargs
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
        **kwargs: Any,
    ):
        """异步按分片流式返回回答。"""
        del stop, kwargs
        content, call = self._decide(messages)
        if call is not None:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_calls=[call]))
            return
        for i in range(0, len(content), 4):
            yield ChatGenerationChunk(message=AIMessageChunk(content=content[i : i + 4]))


def _settings(**subagent_kwargs: Any) -> AppSettings:
    """构造测试配置（mock 模型 + mock embedding + 可选 subagent 覆盖）。"""
    settings = AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 64},
    )
    if subagent_kwargs:
        settings.subagent = settings.subagent.model_copy(update=subagent_kwargs)
    return settings


def _build_env(
    tmp_path: Any,
    settings: AppSettings,
    model: FakeModel,
) -> dict[str, Any]:
    """构造隔离的 RAG、记忆、工具、审批与子 Agent 环境。"""
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
    manager = SubAgentManager(
        chat_model=model,
        memory_store=memory_store,
        tool_runtime=tool_runtime,
        settings=settings,
        workspace=tmp_path,
    )
    tool_runtime.subagent_manager = manager
    graph = build_super_graph(
        rag_store=rag_store,
        chat_model=model,
        memory_store=memory_store,
        settings=settings,
        tool_runtime=tool_runtime,
    )
    return {
        "manager": manager,
        "runtime": tool_runtime,
        "approval": approval_manager,
        "permissions": permission_store,
        "root": tmp_path,
        "log": tool_log,
        "memory": memory_store,
        "settings": settings,
        "model": model,
        "graph": graph,
    }


async def _run_graph_with_events(graph: Any, state: dict[str, Any], runtime: ToolRuntime) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """运行图并消费事件；遇到审批自动 allow_once。"""
    queue: asyncio.Queue = asyncio.Queue()
    runtime.attach("t1", lambda name, data: queue.put_nowait((name, data)))
    events: list[tuple[str, dict[str, Any]]] = []
    final: dict[str, Any] = {}

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
        if name == "approval_requested":
            runtime.approval_manager.submit(data["thread_id"], data["id"], "allow_once")
    await task
    while not queue.empty():
        events.append(queue.get_nowait())
    runtime.detach("t1")
    return final, events


# ---------------------------------------------------------------- HTTP 接口


def test_post_agents_sync_success_and_queries(tmp_path: Any, monkeypatch: Any) -> None:
    """POST /agents 同步成功；GET /agents、GET /agents/{id}、404 正常。"""
    model = FakeModel(sub_answer="子任务完成")
    monkeypatch.setattr("minic.core.server.create_chat_model", lambda s: model)
    short_memory_dir = tmp_path / "short_memory"
    app = create_app(
        settings=_settings(),
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=short_memory_dir,
    )
    with TestClient(app) as client:
        response = client.post("/agents", headers=AUTH_HEADERS, json={"task": "写一段总结"})
        assert response.status_code == 200
        data = response.json()
        assert data["subagent_id"]
        assert data["status"] == "completed"
        assert data["output"] == "子任务完成"

        listing = client.get("/agents", headers=AUTH_HEADERS)
        assert listing.status_code == 200
        assert any(entry["subagent_id"] == data["subagent_id"] for entry in listing.json()["agents"])

        detail = client.get(f"/agents/{data['subagent_id']}", headers=AUTH_HEADERS)
        assert detail.status_code == 200
        assert detail.json()["status"] == "completed"

        missing = client.get("/agents/not-exist", headers=AUTH_HEADERS)
        assert missing.status_code == 404
        assert "NOT_FOUND" in missing.text

        # 子任务不写入主会话 short_memory
        assert list(short_memory_dir.iterdir()) == []


def test_post_agents_empty_task_400(tmp_path: Any, monkeypatch: Any) -> None:
    """task 为空返回 400。"""
    monkeypatch.setattr("minic.core.server.create_chat_model", lambda s: FakeModel())
    app = create_app(
        settings=_settings(),
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "short_memory",
    )
    with TestClient(app) as client:
        response = client.post("/agents", headers=AUTH_HEADERS, json={"task": "  "})
        assert response.status_code == 400
        assert "VALIDATION_ERROR" in response.text


def test_get_agents_empty(tmp_path: Any, monkeypatch: Any) -> None:
    """未运行过子任务时 GET /agents 返回空列表。"""
    monkeypatch.setattr("minic.core.server.create_chat_model", lambda s: FakeModel())
    app = create_app(
        settings=_settings(),
        token="test-token",
        project_root=tmp_path,
        rag_data_dir=tmp_path / "rag-data",
        short_memory_dir=tmp_path / "short_memory",
    )
    with TestClient(app) as client:
        response = client.get("/agents", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.json()["agents"] == []


# ---------------------------------------------------------------- 子任务内工具与审批


def test_subagent_internal_tool_approval_carries_subagent_id(tmp_path: Any) -> None:
    """子任务内调用 Read（allowed_tools 内）走现有审批，approval_requested 带 subagent_id。"""
    settings = _settings()
    settings.approval.workspace_read_auto_approve = False  # 强制 Read 需要审批
    target = tmp_path / "a.txt"
    target.write_text("hello subagent", encoding="utf-8")
    model = FakeModel(sub_decisions=[{"tool": "Read", "args": {"path": str(target)}}])
    env = _build_env(tmp_path, settings, model)

    async def main() -> None:
        queue: asyncio.Queue = asyncio.Queue()

        def emit(name: str, data: dict[str, Any]) -> None:
            queue.put_nowait((name, data))

        task = asyncio.create_task(
            env["manager"].run(task="读取文件内容", allowed_tools=["Read"], budget=4, emit=emit)
        )
        approval_data: dict[str, Any] = {}
        while True:
            name, data = await asyncio.wait_for(queue.get(), timeout=5)
            if name == "approval_requested":
                approval_data = data
                env["approval"].submit(data["thread_id"], data["id"], "allow_once")
                break
        result = await task
        assert result["status"] == "completed"
        assert result["output"] == "子任务完成"
        assert approval_data.get("subagent_id")
        # 子任务内部审批：thread_id 与 subagent_id 一致，可供 /threads/{subagent_id}/approve 区分
        assert approval_data["thread_id"] == approval_data["subagent_id"]
        assert approval_data["subagent_id"] == result["subagent_id"]
        # 工具确实执行了：模型第二轮应看到工具结果文本（拼在 Human 消息里）
        tool_text = ""
        for call_messages in model.captured:
            for message in call_messages:
                content = str(getattr(message, "content", "") or "")
                if "hello subagent" in content:
                    tool_text = content
        assert "hello subagent" in tool_text

    asyncio.run(main())


def test_subagent_denied_tool_continues_and_answers(tmp_path: Any) -> None:
    """子任务内工具被 deny 后继续循环，模型仍可给出最终回答。"""
    settings = _settings()
    settings.approval.workspace_read_auto_approve = False
    target = tmp_path / "a.txt"
    target.write_text("secret", encoding="utf-8")
    model = FakeModel(sub_decisions=[{"tool": "Read", "args": {"path": str(target)}}])
    env = _build_env(tmp_path, settings, model)

    async def main() -> None:
        queue: asyncio.Queue = asyncio.Queue()

        def emit(name: str, data: dict[str, Any]) -> None:
            queue.put_nowait((name, data))

        task = asyncio.create_task(
            env["manager"].run(task="读取文件内容", allowed_tools=["Read"], budget=4, emit=emit)
        )
        while True:
            name, data = await asyncio.wait_for(queue.get(), timeout=5)
            if name == "approval_requested":
                env["approval"].submit(data["thread_id"], data["id"], "deny")
                break
        result = await task
        assert result["status"] == "completed"
        assert result["output"] == "子任务完成"

    asyncio.run(main())


# ---------------------------------------------------------------- 并发与超时


def test_max_concurrent_one_serializes_subagents(tmp_path: Any) -> None:
    """max_concurrent=1 时两个子任务串行执行。"""
    settings = _settings(max_concurrent=1)
    events: list[str] = []
    active = 0
    max_active = 0

    async def observe() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        events.append("start")
        await asyncio.sleep(0.05)
        active -= 1
        events.append("end")

    model = FakeModel(on_invoke=observe)
    env = _build_env(tmp_path, settings, model)

    async def main() -> None:
        await asyncio.gather(
            env["manager"].run(task="任务一", budget=2),
            env["manager"].run(task="任务二", budget=2),
        )

    asyncio.run(main())
    assert max_active == 1, "max_concurrent=1 时不允许并发"
    assert events == ["start", "end", "start", "end"]


def test_max_concurrent_two_allows_overlap(tmp_path: Any) -> None:
    """max_concurrent=2 时两个子任务允许重叠执行。"""
    settings = _settings(max_concurrent=2)
    active = 0
    max_active = 0

    async def observe() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1

    model = FakeModel(on_invoke=observe)
    env = _build_env(tmp_path, settings, model)

    async def main() -> None:
        await asyncio.gather(
            env["manager"].run(task="任务一", budget=2),
            env["manager"].run(task="任务二", budget=2),
        )

    asyncio.run(main())
    assert max_active == 2


def test_subagent_timeout_returns_failed(tmp_path: Any) -> None:
    """子任务超过 timeout_seconds 后返回 failed「子任务超时」。"""
    settings = _settings(timeout_seconds=0.1)
    model = FakeModel(sub_answer="太慢了", sleep_seconds=0.5)
    env = _build_env(tmp_path, settings, model)

    result = asyncio.run(env["manager"].run(task="睡觉", budget=2))
    assert result["status"] == "failed"
    assert result["output"] == "子任务超时"
    assert result["subagent_id"]
    # 状态查询已记录该失败结果
    assert env["manager"].status()[0]["status"] == "failed"


# ---------------------------------------------------------------- 长期记忆只读


def test_subagent_memory_read_only_injected(tmp_path: Any) -> None:
    """子任务系统提示含合并记忆，且不触发记忆提取写入。"""
    settings = _settings()
    env = _build_env(tmp_path, settings, FakeModel())
    env["memory"].add_topic("global", None, "姓名", "张三")
    env["memory"].add_topic("project", str(tmp_path), "项目约定", "用 Python")
    before = env["memory"].read("merged", str(tmp_path))

    asyncio.run(env["manager"].run(task="总结我的信息", budget=2))

    system_texts = []
    for call_messages in env["model"].captured:
        if call_messages:
            content = call_messages[0].content
            if isinstance(content, str):
                system_texts.append(content)
    assert any("张三" in text for text in system_texts), "系统提示应注入合并记忆"
    assert any("用 Python" in text for text in system_texts)
    after = env["memory"].read("merged", str(tmp_path))
    assert after["content"] == before["content"], "子任务不得写入长期记忆"


# ---------------------------------------------------------------- DelegateToSubagent


def test_delegate_tool_registered_in_registry() -> None:
    """DelegateToSubagent 注册进工具注册表。"""
    spec = get_tool("DelegateToSubagent")
    assert spec is not None
    assert spec.description
    assert "task" in spec.args_schema
    assert "allowed_tools" in spec.args_schema
    assert "budget" in spec.args_schema


def test_tool_loop_delegates_to_subagent_and_result_enters_tool_context(tmp_path: Any) -> None:
    """tool_loop 里模型决策调用 DelegateToSubagent，结果进入 tool_context。"""
    settings = _settings()
    model = FakeModel(
        main_decisions=[
            {"tool": "DelegateToSubagent", "args": {"task": "总结项目", "allowed_tools": [], "budget": 2}},
            {"tool": None},
        ]
    )
    env = _build_env(tmp_path, settings, model)
    state: dict[str, Any] = {
        "thread_id": "t1",
        "workspace": str(tmp_path),
        "project_root": str(tmp_path),
        "user_message": "帮我总结项目",
        "history": [],
        "run_id": "run-1",
        "message_id": "msg-1",
    }

    async def main() -> None:
        final, events = await _run_graph_with_events(env["graph"], state, env["runtime"])
        return final, events

    final, events = asyncio.run(main())
    names = [name for name, _ in events]
    assert "approval_requested" in names  # DelegateToSubagent 默认需要审批
    tool_context = final["tool_context"]
    assert tool_context[0]["tool"] == "DelegateToSubagent"
    assert tool_context[0]["status"] == "completed"
    assert tool_context[0]["output"] == "子任务完成"
