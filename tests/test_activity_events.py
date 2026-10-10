"""分离公开执行说明与最终答复，同时保持 token 兼容性。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessageChunk

from minic.chat.engine import ChatEngine
from minic.chat.memory import ShortMemoryStore
from minic.tools.runtime import ToolRuntime


def drive(awaitable):
    """推进没有外部等待的测试协程，无需建立 Windows socketpair。"""
    try:
        awaitable.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("测试不应执行外部异步 IO")


def test_engine_emits_activity_tokens_and_authoritative_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    from langchain_core.messages import AIMessage
    class Graph:
        values = {}
        async def aget_state(self, *args, **kwargs):
            return SimpleNamespace(values=self.values, tasks=(), next=(), interrupts=())
        async def astream(self, graph_input, *args, **kwargs):
            yield ((), "messages", (AIMessageChunk(content="我先检查。"), {"langgraph_node": "agent"}))
            self.values = {**graph_input, "answer": "检查完成。", "sources": [],
                           "messages": graph_input["messages"] + [AIMessage(content="检查完成。", id="answer")]}
            yield ((), "values", self.values)
    memory = ShortMemoryStore(tmp_path / "memory")
    memory.create("thread", str(tmp_path))
    engine = ChatEngine(None, memory, Graph())
    async def collect():
        return [event async for event in engine.stream_chat("thread", str(tmp_path), str(tmp_path), "检查", "run", "message")]
    events = asyncio.run(collect())
    token = next(data for name, data in events if name == "token")
    assert token["channel"] == "activity"
    assert next(data for name, data in events if name == "answer")["text"] == "检查完成。"
    assert [name for name, _ in events][-3:] == ["answer", "message_end", "done"]


def test_approval_events_use_the_original_tool_call_id(tmp_path):
    events = []

    class Approvals:
        workspace = tmp_path
        def plan(self, *args):
            return "approval"
        def request(self, *args, **kwargs):
            return SimpleNamespace(id="approval", tool_call_id="different", subagent_id=None,
                                   tool="Write", args={}, options=["deny"], decision="deny")
        async def wait(self, approval_id):
            pass

    runtime = ToolRuntime(Approvals(), None, SimpleNamespace(append=lambda data: None))
    runtime._existing_result = lambda *args: None
    drive(runtime.execute(thread_id="thread", run_id="run", tool="Write", args={}, tool_call_id="original",
                      emit=lambda name, data: events.append((name, data))))
    requested = next(data for name, data in events if name == "approval_requested")
    result = next(data for name, data in events if name == "approval_result")
    assert requested["tool_call_id"] == result["tool_call_id"] == "original"
