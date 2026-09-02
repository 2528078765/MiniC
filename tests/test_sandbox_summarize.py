"""G10 中间件补齐测试：摘要（Summarization）+ 沙箱策略（SandboxPolicy）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.callbacks.manager import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from minic.chat.engine import ChatEngine
from minic.chat.memory import ShortMemoryStore
from minic.chat.models import MockChatModel, create_chat_model
from minic.core.config import AppSettings
from minic.graph import build_super_graph
from minic.graph.common import build_messages
from minic.memory import LongTermMemoryStore
from minic.middleware import SandboxPolicy, estimate_chars, summarize_history
from minic.rag.embeddings import MockEmbeddingProvider
from minic.rag.store import RagStore
from minic.recovery import ToolExecutionLog
from minic.tools.runtime import ToolRuntime
from minic.tools.service import ApprovalManager, PermissionStore, ToolExecutor

HEADERS = {"Authorization": "Bearer test-token"}


class FakeSummarizeModel(BaseChatModel):


    def bind_tools(self, tools, **kwargs):
        """忽略工具绑定（mock）。"""
        del tools, kwargs
        return self
    """测试用模型：识别总图/记忆/工具/摘要提示，并记录实际收到的消息。"""

    model_name: str = "mock-summarize"
    temperature: float = 0.0
    summary: str = Field(default="这是生成的历史摘要。")
    invoke_captured: list[list[BaseMessage]] = Field(default_factory=list)
    stream_captured: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "mock-summarize"

    def _mock_content(self, messages: list[BaseMessage]) -> str:
        self.invoke_captured.append(list(messages))
        system = messages[0].content
        if "意图路由" in system:
            return json.dumps({"intent": "chat_action"})
        if "记忆提取" in system:
            return "[]"
        if "可用工具" in system:
            human_text = "".join(str(m.content) for m in messages if isinstance(m, HumanMessage))
            return "摘要回答。" if "历史摘要" in human_text else "普通回答。"
        if "总结" in system:
            return self.summary
        return "ok"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self._mock_content(messages)))]
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs,
    ):
        del stop, run_manager, kwargs
        content = self._mock_content(messages)
        for part in self._answer_parts(messages, content):
            yield ChatGenerationChunk(message=AIMessageChunk(content=part))

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs,
    ):
        del stop, kwargs
        content = self._mock_content(messages)
        for part in self._answer_parts(messages, content):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=part))
            if run_manager is not None:
                await run_manager.on_llm_new_token(part, chunk=chunk.message)
            yield chunk

    def _answer_parts(self, messages: list[BaseMessage], content: str) -> list[str]:
        self.stream_captured.append(list(messages))
        if content != "ok":
            return [content]
        human_text = "".join(str(m.content) for m in messages if isinstance(m, HumanMessage))
        return ["摘要回答。"] if "历史摘要" in human_text else ["普通回答。"]


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析成事件列表。"""
    events = []
    event_name = None
    data_lines = []
    for line in text.splitlines():
        if line == "":
            if event_name and data_lines:
                events.append((event_name, json.loads("\n".join(data_lines))))
            event_name = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    return events


def _token_text(events: list[tuple[str, dict]]) -> str:
    """拼接所有 token 事件文本。"""
    return "".join(data["delta"] for name, data in events if name == "token")


def _long_history(message_count: int = 30, per_message_chars: int = 200) -> list[dict]:
    """构造超长历史消息列表。"""
    messages = []
    for index in range(message_count):
        messages.append({"role": "human", "content": f"第 {index} 条历史消息" + "长" * per_message_chars})
        messages.append({"role": "ai", "content": f"第 {index} 条回答" + "长" * per_message_chars})
    return messages


# ---------- 摘要：单元 ----------


def test_estimate_chars() -> None:
    """历史文本总字符数正确。"""
    messages = [{"role": "human", "content": "abc"}, {"role": "ai", "content": "defgh"}]
    assert estimate_chars(messages) == 8


def test_summarize_history_over_threshold_calls_model() -> None:
    """超阈值时调用模型（system prompt 含总结、输入为历史文本）并返回摘要历史。"""
    model = FakeSummarizeModel()
    messages = [{"role": "human", "content": "长" * 10000}]
    result = asyncio.run(summarize_history(messages, model, threshold_chars=100))

    assert result[0]["content"].startswith("历史摘要")
    assert "这是生成的历史摘要。" in result[0]["content"]
    assert result[-1] == messages[-1]  # 保留最后一条（本次用户消息）
    summarize_msgs = [msgs for msgs in model.invoke_captured if "总结以下对话" in msgs[0].content]
    assert summarize_msgs, "超阈值时应调用摘要模型"
    assert "长" * 10 in summarize_msgs[0][1].content  # 输入为历史文本


def test_summarize_history_under_threshold_returns_original() -> None:
    """未超阈值时返回原 history 且不调用模型。"""
    model = FakeSummarizeModel()
    messages = [{"role": "human", "content": "hi"}, {"role": "ai", "content": "hello"}]
    result = asyncio.run(summarize_history(messages, model, threshold_chars=1000))
    assert result == messages
    assert model.invoke_captured == []


def test_summarize_history_failure_degrades_to_truncation() -> None:
    """模型抛异常时降级为截断且不抛错。"""
    class BrokenModel:
        async def ainvoke(self, messages: list) -> None:
            del messages
            raise RuntimeError("模型不可用")

    messages = [{"role": "human", "content": "内容" * 5000}]
    result = asyncio.run(summarize_history(messages, BrokenModel(), threshold_chars=100))
    assert "（历史过长已截断）" in result[0]["content"]
    assert result[-1] == messages[-1]


def test_summarize_history_summary_injected_into_answer_messages() -> None:
    """摘要历史经 build_messages 注入回答节点，全量历史不再出现。"""
    model = FakeSummarizeModel()
    messages = [{"role": "human", "content": "长" * 10000}]
    summarized = asyncio.run(summarize_history(messages, model, threshold_chars=100))
    built = build_messages("system", summarized, "当前问题")
    text = "".join(str(m.content) for m in built)
    assert "历史摘要" in text
    assert "这是生成的历史摘要。" in text
    assert "长" * 10000 not in text


# ---------- 摘要：ChatEngine 集成 ----------


def _build_engine(tmp_path: Path, model: FakeSummarizeModel, threshold: int):
    """构造带摘要回调的 ChatEngine 与含长历史的会话。"""
    settings = AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 64},
    )
    memory_store = LongTermMemoryStore(tmp_path / "global-memory", tmp_path)
    rag_store = RagStore(tmp_path / "rag-data", settings, MockEmbeddingProvider(64))
    graph = build_super_graph(rag_store, model, memory_store, settings)
    short_memory = ShortMemoryStore(tmp_path / "memory" / "short_memory")
    thread_id = "sum-1"
    short_memory.create(thread_id, str(tmp_path))
    for message in _long_history():
        short_memory.append_message(thread_id, message["role"], message["content"])
    engine = ChatEngine(
        rag_store=rag_store,
        short_memory=short_memory,
        graph=graph,
        summarize_history=lambda messages: summarize_history(messages, model, threshold),
    )
    return engine, thread_id


async def _collect(engine: ChatEngine, thread_id: str, workspace: Path) -> list[tuple[str, dict]]:
    """运行 stream_chat 并收集事件。"""
    events = []
    async for name, data in engine.stream_chat(
        thread_id=thread_id,
        workspace=str(workspace),
        project_root=str(workspace),
        message="当前问题",
        run_id="run-1",
        message_id="msg-1",
    ):
        events.append((name, data))
    return events


def test_engine_over_threshold_summarizes_into_answer(tmp_path: Path) -> None:
    """历史超阈值时摘要注入回答节点（回答基于摘要），模型收到总结提示。"""
    model = FakeSummarizeModel()
    engine, thread_id = _build_engine(tmp_path, model, threshold=100)
    events = asyncio.run(_collect(engine, thread_id, tmp_path))

    assert "摘要回答。" in _token_text(events)
    summarize_msgs = [msgs for msgs in model.invoke_captured if "总结以下对话" in msgs[0].content]
    assert summarize_msgs, "超阈值时应调用摘要模型"
    assert "第 15 条历史消息" in summarize_msgs[0][1].content


def test_engine_under_threshold_does_not_summarize(tmp_path: Path) -> None:
    """未超阈值时模型未被摘要调用，回答基于原始历史。"""
    model = FakeSummarizeModel()
    engine, thread_id = _build_engine(tmp_path, model, threshold=10_000_000)
    events = asyncio.run(_collect(engine, thread_id, tmp_path))

    assert "普通回答。" in _token_text(events)
    summarize_msgs = [msgs for msgs in model.invoke_captured if "总结以下对话" in msgs[0].content]
    assert summarize_msgs == []


def test_engine_summarize_failure_does_not_break_stream(tmp_path: Path) -> None:
    """摘要模型抛异常时降级截断，SSE 流仍正常结束。"""

    class BrokenModel(FakeSummarizeModel):
        async def ainvoke(self, messages: list) -> AIMessage:
            system = messages[0].content
            if "总结" in system:
                raise RuntimeError("摘要失败")
            return await super().ainvoke(messages)

    model = BrokenModel()
    engine, thread_id = _build_engine(tmp_path, model, threshold=100)
    events = asyncio.run(_collect(engine, thread_id, tmp_path))

    names = [name for name, _ in events]
    assert names[-2:] == ["message_end", "done"]
    assert "普通回答。" in _token_text(events)  # 截断历史仍走回答，不抛错


# ---------- 沙箱：ToolRuntime 路径强制 ----------


def _tool_env(tmp_path: Path) -> dict:
    """构造隔离的工具环境，注入默认沙箱策略。"""
    settings = AppSettings(
        model={"provider": "mock"},
        embedding={"provider": "mock", "dimension": 64},
    )
    memory_store = LongTermMemoryStore(tmp_path / "global-memory", tmp_path)
    permission_store = PermissionStore(
        tmp_path / "global-permissions.json",
        tmp_path / ".minic" / "permissions.json",
    )
    executor = ToolExecutor(tmp_path, tmp_path / ".minic" / "backups" / "files", memory_store)
    approval_manager = ApprovalManager(permission_store, tmp_path, settings)
    tool_log = ToolExecutionLog(tmp_path / ".minic" / "logs" / "tool_execution.jsonl")
    runtime = ToolRuntime(
        approval_manager,
        executor,
        tool_log,
        sandbox_policy=SandboxPolicy(),
    )
    return {"runtime": runtime, "approval": approval_manager, "root": tmp_path}


async def _run_with_decision(
    runtime: ToolRuntime,
    thread_id: str,
    tool: str,
    args: dict,
    decision: str | None = None,
):
    """并发消费工具事件，审批时按 decision 提交。"""
    queue: asyncio.Queue = asyncio.Queue()
    runtime.attach(thread_id, lambda name, data: queue.put_nowait((name, data)))
    events: list[tuple[str, dict]] = []

    async def execute() -> dict:
        return await runtime.execute(
            thread_id=thread_id,
            run_id="run-1",
            tool=tool,
            args=args,
        )

    task = asyncio.create_task(execute())
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
    return task.result(), events


def test_sandbox_denies_write_outside_workspace(tmp_path: Path) -> None:
    """工作区外 Write 被沙箱强制拒绝，不进入审批。"""
    env = _tool_env(tmp_path)
    outside = tmp_path.parent / "g10-outside.txt"
    result, events = asyncio.run(
        _run_with_decision(env["runtime"], "t1", "Write", {"path": str(outside), "content": "x"})
    )
    assert result["status"] == "denied"
    assert "沙箱策略限制" in result["output"]
    names = [name for name, _ in events]
    assert "approval_requested" not in names
    assert "tool_result" in names
    assert not outside.exists()


def test_sandbox_denies_read_outside_workspace(tmp_path: Path) -> None:
    """工作区外 Read 被沙箱强制拒绝，即使设置了 allow_always 权限。"""
    env = _tool_env(tmp_path)
    outside = tmp_path.parent / "g10-outside-read.txt"
    outside.write_text("secret", encoding="utf-8")
    env["approval"].permission_store.grant("project", tmp_path, "Read", str(outside), "allow_always")
    result, events = asyncio.run(
        _run_with_decision(env["runtime"], "t2", "Read", {"path": str(outside)})
    )
    assert result["status"] == "denied"
    assert "沙箱策略限制" in result["output"]
    assert "approval_requested" not in [name for name, _ in events]


def test_sandbox_read_inside_workspace_auto_ok(tmp_path: Path) -> None:
    """工作区内 Read 正常执行。"""
    env = _tool_env(tmp_path)
    inside = tmp_path / "a.txt"
    inside.write_text("hello", encoding="utf-8")
    result, events = asyncio.run(
        _run_with_decision(env["runtime"], "t3", "Read", {"path": str(inside)})
    )
    assert result["status"] == "completed"
    assert result["output"] == "hello"
    assert "approval_requested" not in [name for name, _ in events]


def test_sandbox_workspace_write_still_requires_approval(tmp_path: Path) -> None:
    """工作区内 Write 不被沙箱误拦，仍走审批。"""
    env = _tool_env(tmp_path)
    inside = tmp_path / "a.txt"
    inside.write_text("old", encoding="utf-8")
    result, events = asyncio.run(
        _run_with_decision(
            env["runtime"], "t4", "Write", {"path": str(inside), "content": "new"}, "allow_once"
        )
    )
    assert result["status"] == "completed"
    assert inside.read_text(encoding="utf-8") == "new"
    names = [name for name, _ in events]
    assert "approval_requested" in names
    assert "tool_result" in names


def test_sandbox_allows_text_search_inside_workspace(tmp_path: Path) -> None:
    """工作区内 TextSearch 正常；工作区外 path 被拒。"""
    env = _tool_env(tmp_path)
    (tmp_path / "b.txt").write_text("keyword here", encoding="utf-8")
    result, _ = asyncio.run(
        _run_with_decision(env["runtime"], "t5", "TextSearch", {"path": str(tmp_path), "pattern": "keyword"})
    )
    assert result["status"] == "completed"
    assert "keyword here" in result["output"]

    outside = tmp_path.parent / "outside-dir"
    outside.mkdir(exist_ok=True)
    (outside / "c.txt").write_text("keyword", encoding="utf-8")
    result, _ = asyncio.run(
        _run_with_decision(env["runtime"], "t6", "TextSearch", {"path": str(outside), "pattern": "keyword"})
    )
    assert result["status"] == "denied"
    assert "沙箱策略限制" in result["output"]


def test_sandbox_mcp_and_subagent_tools_not_checked(tmp_path: Path) -> None:
    """MCP 工具（含 .）与 DelegateToSubagent 不检查。"""
    policy = SandboxPolicy()
    assert policy.check_tool("srv.any_tool", {}, tmp_path) is None
    assert policy.check_tool("DelegateToSubagent", {"task": "hi"}, tmp_path) is None


# ---------- 沙箱：Bash 危险命令 ----------


def test_sandbox_bash_dangerous_commands_rejected() -> None:
    """Bash 高危命令被黑名单拒绝。"""
    policy = SandboxPolicy()
    for command in ("rm -rf /", "rm -rf /var", "rm -rf ~", "format c:", "shutdown /s /t 0", "taskkill /f /im python.exe"):
        assert policy.check_tool("Bash", {"command": command}, "C:/ws") is not None


def test_sandbox_bash_normal_commands_allowed() -> None:
    """普通 Bash 命令不受影响。"""
    policy = SandboxPolicy()
    for command in ("echo hi", "ls -la", "git status", "python run.py"):
        assert policy.check_tool("Bash", {"command": command}, "C:/ws") is None


def test_sandbox_bash_dangerous_rejected_by_runtime(tmp_path: Path) -> None:
    """Bash 危险命令经 ToolRuntime 直接 denied，不进入审批。"""
    env = _tool_env(tmp_path)
    result, events = asyncio.run(
        _run_with_decision(env["runtime"], "t7", "Bash", {"command": "rm -rf /"})
    )
    assert result["status"] == "denied"
    assert "沙箱策略限制" in result["output"]
    assert "approval_requested" not in [name for name, _ in events]


def test_sandbox_bash_normal_goes_through_approval(tmp_path: Path) -> None:
    """Bash 正常命令仍走审批，不被沙箱误拦。"""
    env = _tool_env(tmp_path)
    marker = tmp_path / "g10-bash-marker.txt"
    result, events = asyncio.run(
        _run_with_decision(
            env["runtime"],
            "t8",
            "Bash",
            {"command": f'echo ok > "{marker}"'},
            "allow_once",
        )
    )
    assert result["status"] == "completed"
    assert marker.exists()
    assert "approval_requested" in [name for name, _ in events]


# ---------- 沙箱：check_network 模型域名白名单 ----------


def test_check_network_whitelist() -> None:
    """白名单内 host 通过，白名单外拒绝。"""
    policy = SandboxPolicy()
    assert policy.check_network("https://api.deepseek.com") is True
    assert policy.check_network("https://api.deepseek.com/v1/chat") is True
    assert policy.check_network("https://dashscope.aliyuncs.com") is True
    assert policy.check_network("http://127.0.0.1:11434/v1") is True
    assert policy.check_network("api.deepseek.com") is True  # 裸域名
    assert policy.check_network("http://127.0.0.1:9999") is True  # 127.0.0.1 任意端口
    assert policy.check_network("https://unknown.example.com") is False
    assert policy.check_network("http://evil.com") is False


def test_create_chat_model_rejects_non_whitelisted_host() -> None:
    """白名单外 host 的模型 base_url 启动即失败。"""
    settings = AppSettings(
        model={"provider": "openai", "model": "gpt-4o", "base_url": "https://evil.example.com"}
    )
    with pytest.raises(ValueError, match="沙箱白名单"):
        create_chat_model(settings)


def test_create_chat_model_mock_skips_network_check() -> None:
    """mock provider 不校验网络，返回 MockChatModel。"""
    settings = AppSettings(model={"provider": "mock"})
    registry = create_chat_model(settings)
    assert isinstance(registry.current(), MockChatModel)


# ---------- 沙箱：HTTP 层 ----------


def test_http_sandbox_denies_outside_write(client) -> None:
    """HTTP /tools/run 工作区外 Write 返回 denied「沙箱策略限制」，无审批事件。"""
    outside = Path.cwd().parent / "g10-sandbox-outside.txt"
    response = client.post(
        "/tools/run",
        headers=HEADERS,
        json={"tool": "Write", "args": {"path": str(outside), "content": "x"}},
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert "approval_requested" not in names
    tool_results = [data for name, data in events if name == "tool_result"]
    assert tool_results
    assert tool_results[0]["status"] == "denied"
    assert "沙箱策略限制" in tool_results[0]["output"]
    assert not outside.exists()


def test_http_sandbox_blocks_dangerous_bash(client) -> None:
    """HTTP /tools/run Bash 危险命令被拒，不进入审批。"""
    response = client.post(
        "/tools/run",
        headers=HEADERS,
        json={"tool": "Bash", "args": {"command": "format c:"}},
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert "approval_requested" not in names
    tool_results = [data for name, data in events if name == "tool_result"]
    assert tool_results
    assert tool_results[0]["status"] == "denied"
    assert "沙箱策略限制" in tool_results[0]["output"]
