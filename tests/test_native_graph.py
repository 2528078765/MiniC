"""原生状态、跨重启审批恢复、稳定副作用与可编辑项目记忆回归。"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from minic.chat.engine import ChatEngine
from minic.chat.memory import ShortMemoryStore
from minic.core.config import AppSettings
from minic.graph import build_super_graph
from minic.memory import LongTermMemoryStore
from minic.recovery import ToolExecutionLog
from minic.tools.runtime import ToolRuntime
from minic.tools.service import ApprovalManager, PermissionStore, ToolExecutor
from minic.subagent import SubAgentManager

class ScriptModel:
    def __init__(self, delegate=False):
        self.prompts = []
        self.delegate = delegate
    def bind_tools(self, tools):
        self.tools = tools
        return self
    async def ainvoke(self, messages):
        system = str(messages[0].content)
        if "意图路由" in system:
            return AIMessage(content=json.dumps({"intent": "chat_action"}))
        if "记忆提取" in system:
            return AIMessage(content="[]")
        self.prompts.append(messages)
        if "子 Agent" in system and not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="", tool_calls=[{"name": "Write", "args": {"path": "child.txt", "content": "child"}, "id": "child-write"}])
        return AIMessage(content="子任务完成")
    async def astream(self, messages):
        self.prompts.append(messages)
        completed = [m for m in messages if isinstance(m, ToolMessage)]
        current_human = next(m for m in reversed(messages) if m.type == "human")
        if current_human.content == "run" and not completed:
            calls = ([{"name": "DelegateToSubagent", "args": {"task": "写入 child.txt", "allowed_tools": ["Write"], "budget": 2}, "id": "delegate"}]
                     if self.delegate else [
                         {"name": "Read", "args": {"path": "input.txt"}, "id": "read-1"},
                         {"name": "Write", "args": {"path": "output.txt", "content": "saved"}, "id": "write-1"}])
            yield AIMessageChunk(content="", tool_calls=calls, additional_kwargs={"reasoning_content": "test metadata"})
        else:
            system = str(messages[0].content)
            yield AIMessageChunk(content="已完成；memory-new" if "memory-new" in system else "已完成；memory-old")

def environment(root, model, saver):
    root.mkdir(exist_ok=True)
    settings = AppSettings(model={"provider": "mock"}, embedding={"provider": "mock"})
    settings.approval.workspace_write_auto_approve = False
    memory = LongTermMemoryStore(root / "global-memory", root)
    manager = ApprovalManager(PermissionStore(root / "global-permissions.json", root / ".minic" / "permissions.json"), root, settings)
    executor = ToolExecutor(root, root / ".minic" / "backups", memory)
    runtime = ToolRuntime(manager, executor, ToolExecutionLog(root / "tool.jsonl"))
    runtime.subagent_manager = SubAgentManager(model, memory, runtime, settings, root)
    rag = SimpleNamespace(inventory=lambda: [], settings=settings)
    graph = build_super_graph(rag, model, memory, settings, runtime, checkpointer=saver)
    short = ShortMemoryStore(root / "memory" / "short")
    if not short.load("thread", str(root)):
        short.create("thread", str(root))
    return ChatEngine(rag, short, graph, runtime), memory, runtime

async def collect(engine, root, message=None, resume=False, run="run-1", mid="message-1"):
    message = ("" if resume else "run") if message is None else message
    return [event async for event in engine.stream_chat("thread", str(root), str(root), message, run, mid, resume)]

def test_native_restart_approval_results_and_live_memory(tmp_path):
    async def scenario():
        (tmp_path / "input.txt").write_text("fixture", encoding="utf-8")
        legacy = tmp_path / ".minic" / "memory" / "minic.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("# 约定\nmemory-old", encoding="utf-8")
        database = str(tmp_path / "state.sqlite")
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            engine, memory, runtime = environment(tmp_path, ScriptModel(), saver)
            events = await collect(engine, tmp_path)
            assert events[-1][1]["status"] == "waiting_approval", events
            assert not (tmp_path / "output.txt").exists()
            assert memory.ensure_project(str(tmp_path)).read_text(encoding="utf-8") == legacy.read_text(encoding="utf-8")
            pending = (await engine.execution_state("thread", str(tmp_path)))["pending_approvals"][0]
            state = await engine.state("thread", str(tmp_path))
            assert state.next
            # No in-memory approval is needed to resume after a process restart.
            assert not runtime.approval_manager.pending
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            model = ScriptModel()
            engine, memory, runtime = environment(tmp_path, model, saver)
            resumed = await engine.execution_state("thread", str(tmp_path))
            assert resumed["pending_approvals"][0]["id"] == pending["id"]
            await engine.approve("thread", str(tmp_path), pending["id"], "allow_once")
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            engine, memory, runtime = environment(tmp_path, model, saver)
            events = await collect(engine, tmp_path, resume=True)
            assert events[-1][1]["status"] == "completed", events
            assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "saved"
            intents = [r for r in runtime.tool_log.read() if r["event"] == "intent"]
            assert [r["tool"] for r in intents].count("Read") == 1
            assert [r["tool"] for r in intents].count("Write") == 1
            values = (await engine.state("thread", str(tmp_path))).values
            calls = [m for m in values["messages"] if isinstance(m, AIMessage) and m.tool_calls]
            assert calls[0].additional_kwargs["reasoning_content"] == "test metadata"
            results = [m.tool_call_id for m in values["messages"] if isinstance(m, ToolMessage)]
            assert results == ["read-1", "write-1"]
            assert next(m.content for m in values["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "read-1") == "fixture"
            (tmp_path / "Agent.md").write_text("# 约定\nmemory-new", encoding="utf-8")
            events = await collect(engine, tmp_path, "next", run="run-2", mid="message-2")
            assert events[-1][1]["status"] == "completed", events
            assert any(name == "answer" and "memory-new" in data["text"] for name, data in events)
            messages = (await engine.state("thread", str(tmp_path))).values["messages"]
            assert [m.content for m in messages if m.type == "human"] == ["run", "next"]
            assert all(m.type != "system" for m in messages)
            assert any(name == "graph_step" and data["namespace"] for name, data in events)
            assert legacy.read_text(encoding="utf-8") == "# 约定\nmemory-old"
    asyncio.run(scenario())

def test_existing_agent_structure_and_old_memory_preserved(tmp_path):
    content = "人工前言\n\n# 工作约定\nPython\n\n## 代码范例\n```python\n# 保留注释\nprint('yes')\n```\n"
    (tmp_path / "Agent.md").write_text(content, encoding="utf-8")
    memory = LongTermMemoryStore(tmp_path / "global", tmp_path)
    assert memory.ensure_project() == tmp_path / "Agent.md"
    assert (tmp_path / "Agent.md").read_text(encoding="utf-8") == content
    memory.add_topic("project", str(tmp_path), "测试命令", "pytest", source="user")
    updated = (tmp_path / "Agent.md").read_text(encoding="utf-8")
    assert content.rstrip() in updated
    assert "# 测试命令\npytest" in updated
    from minic.memory import parse_topics
    assert "保留注释" not in parse_topics(updated)
    assert "# 保留注释" in parse_topics(updated)["代码范例"]
    memory.write("project", str(tmp_path), "# 新约定\n保留结构", mode="merge")
    memory.mark_deleted("project", str(tmp_path), "测试命令")
    memory.remove_topic("project", str(tmp_path), "新约定")
    assert content.rstrip() in (tmp_path / "Agent.md").read_text(encoding="utf-8")
    assert list((tmp_path / ".minic" / "backups" / "files").glob("*.bak"))

def test_multiple_approvals_keep_independent_decisions(tmp_path):
    class TwoWrites(ScriptModel):
        async def astream(self, messages):
            if not any(isinstance(item, ToolMessage) for item in messages):
                yield AIMessageChunk(content="", tool_calls=[
                    {"name": "Write", "args": {"path": "one.txt", "content": "one"}, "id": "one"},
                    {"name": "Write", "args": {"path": "two.txt", "content": "two"}, "id": "two"},
                ])
            else:
                yield AIMessageChunk(content="两项操作已分别处理")
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.sqlite")) as saver:
            engine, _, runtime = environment(tmp_path, TwoWrites(), saver)
            events = await collect(engine, tmp_path)
            assert events[-1][1]["status"] == "waiting_approval"
            pending = (await engine.execution_state("thread", str(tmp_path)))["pending_approvals"][0]
            assert pending["tool_call_id"] == "one"
            await engine.approve("thread", str(tmp_path), pending["id"], "deny")
            events = await collect(engine, tmp_path, resume=True)
            assert events[-1][1]["status"] == "waiting_approval", events
            assert not (tmp_path / "one.txt").exists()
            assert not (tmp_path / "two.txt").exists()
            pending = (await engine.execution_state("thread", str(tmp_path)))["pending_approvals"][0]
            assert pending["tool_call_id"] == "two"
            await engine.approve("thread", str(tmp_path), pending["id"], "allow_once")
            events = await collect(engine, tmp_path, resume=True)
            assert events[-1][1]["status"] == "completed", events
            assert not (tmp_path / "one.txt").exists()
            assert (tmp_path / "two.txt").read_text(encoding="utf-8") == "two"
            assert len([entry for entry in runtime.tool_log.read() if entry["event"] == "intent"]) == 1
    asyncio.run(scenario())

def test_standalone_subagent_persists_and_resumes(tmp_path):
    async def scenario():
        database = str(tmp_path / "state.sqlite")
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            engine, _, runtime = environment(tmp_path, ScriptModel(), saver)
            manager = runtime.subagent_manager
            manager.checkpointer = saver
            result = await manager.run("write child", ["Write"], budget=2)
            assert result["status"] == "waiting_approval", result
            sid = result["subagent_id"]
            pending = result["pending_approvals"][0]
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            engine, _, runtime = environment(tmp_path, ScriptModel(), saver)
            manager = runtime.subagent_manager
            manager.checkpointer = saver
            assert any(item["subagent_id"] == sid for item in manager.status())
            result = await manager.resume(sid, pending["id"], "allow_once")
            assert result["status"] == "completed", result
            assert (tmp_path / "child.txt").read_text(encoding="utf-8") == "child"
    asyncio.run(scenario())

def test_unknown_effect_needs_new_confirmation_after_allow_always(tmp_path):
    async def scenario():
        (tmp_path / "input.txt").write_text("fixture", encoding="utf-8")
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.sqlite")) as saver:
            engine, _, runtime = environment(tmp_path, ScriptModel(), saver)
            events = await collect(engine, tmp_path)
            first = (await engine.execution_state("thread", str(tmp_path)))["pending_approvals"][0]
            await engine.approve("thread", str(tmp_path), first["id"], "allow_always")
            original = runtime._execute_tool
            executions = []
            async def interrupted(tool, args, *rest):
                result = await original(tool, args, *rest)
                executions.append(tool)
                raise asyncio.CancelledError
            runtime._execute_tool = interrupted
            events = await collect(engine, tmp_path, resume=True)
            # 人为中断工具任务由 LangGraph 包装为节点失败，仍必须保留未知副作用记录。
            assert events[-1][1]["status"] == "failed", [item for item in events if item[0] in {"error", "done"}]
            assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "saved"
            runtime._execute_tool = original
            events = await collect(engine, tmp_path, resume=True)
            assert events[-1][1]["status"] == "waiting_approval", events
            recovery = (await engine.execution_state("thread", str(tmp_path)))["pending_approvals"][0]
            assert recovery["id"] != first["id"]
            assert recovery["options"] == ["allow_once", "deny"]
            assert "结果尚未确认" in recovery["message"]
            await engine.approve("thread", str(tmp_path), recovery["id"], "deny")
            events = await collect(engine, tmp_path, resume=True)
            assert events[-1][1]["status"] == "completed", events
            assert executions == ["Write"]
            assert len([entry for entry in runtime.tool_log.read() if entry["event"] == "intent" and entry["tool"] == "Write"]) == 1
    asyncio.run(scenario())

def test_cancel_bash_kills_process_and_requires_new_confirmation(tmp_path):
    import os
    import shlex
    import subprocess
    import sys
    argv = [sys.executable, '-c', 'import time; time.sleep(30)']
    command = subprocess.list2cmdline(argv) if os.name == 'nt' else shlex.join(argv)
    class BashModel(ScriptModel):
        async def astream(self, messages):
            if not any(isinstance(item, ToolMessage) for item in messages):
                yield AIMessageChunk(content="", tool_calls=[{"name": "Bash", "args": {"command": command}, "id": "bash"}])
            else:
                yield AIMessageChunk(content="已停止命令")
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.sqlite")) as saver:
            engine, _, runtime = environment(tmp_path, BashModel(), saver)
            events = await collect(engine, tmp_path)
            pending = (await engine.execution_state("thread", str(tmp_path)))["pending_approvals"][0]
            await engine.approve("thread", str(tmp_path), pending["id"], "allow_once")
            factory = asyncio.create_subprocess_shell
            captured = []
            async def capture(*args, **kwargs):
                process = await factory(*args, **kwargs)
                captured.append(process)
                return process
            asyncio.create_subprocess_shell = capture
            try:
                consumption = asyncio.create_task(collect(engine, tmp_path, resume=True))
                for _ in range(300):
                    if captured:
                        break
                    await asyncio.sleep(0.01)
                assert captured
                assert captured[0].returncode is None
                await engine.cancel("run-1")
                events = await asyncio.wait_for(consumption, 5)
                assert events[-1][1]["status"] == "cancelled", events
                assert captured[0].returncode is not None
                events = await collect(engine, tmp_path, resume=True)
                assert events[-1][1]["status"] == "waiting_approval", events
                recovery = (await engine.execution_state("thread", str(tmp_path)))["pending_approvals"][0]
                assert recovery["id"] != pending["id"]
                await engine.approve("thread", str(tmp_path), recovery["id"], "deny")
                events = await collect(engine, tmp_path, resume=True)
                assert events[-1][1]["status"] == "completed", events
                assert len(captured) == 1
            finally:
                asyncio.create_subprocess_shell = factory
    asyncio.run(scenario())

def test_nested_subagent_approval_restart(tmp_path):
    async def scenario():
        database = str(tmp_path / "state.sqlite")
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            engine, _, runtime = environment(tmp_path, ScriptModel(delegate=True), saver)
            events = await collect(engine, tmp_path)
            assert events[-1][1]["status"] == "waiting_approval", events
            pending = (await engine.execution_state("thread", str(tmp_path)))["pending_approvals"][0]
            # Delegation itself needs permission; first approval resumes into the child graph.
            await engine.approve("thread", str(tmp_path), pending["id"], "allow_once")
            events = await collect(engine, tmp_path, resume=True)
            assert events[-1][1]["status"] == "waiting_approval", events
            pending = (await engine.execution_state("thread", str(tmp_path)))["pending_approvals"][0]
            assert pending["subagent_id"]
            assert pending["thread_id"] == "thread"
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            engine, _, runtime = environment(tmp_path, ScriptModel(delegate=True), saver)
            await engine.approve("thread", str(tmp_path), pending["id"], "allow_once")
            events = await collect(engine, tmp_path, resume=True)
            assert events[-1][1]["status"] == "completed", events
            assert (tmp_path / "child.txt").read_text(encoding="utf-8") == "child"
            assert len([r for r in runtime.tool_log.read() if r["event"] == "intent" and r["tool"] == "Write"]) == 1
            messages = (await engine.state("thread", str(tmp_path))).values["messages"]
            assert not any(isinstance(m, ToolMessage) and m.tool_call_id == "child-write" for m in messages)
    asyncio.run(scenario())

def test_cancel_drains_graph_task(tmp_path):
    class WaitingModel(ScriptModel):
        async def astream(self, messages):
            self.waiting = True
            await asyncio.sleep(20)
            yield AIMessageChunk(content="never")
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.sqlite")) as saver:
            model = WaitingModel()
            engine, _, _ = environment(tmp_path, model, saver)
            consumption = asyncio.create_task(collect(engine, tmp_path))
            for _ in range(100):
                if getattr(model, "waiting", False):
                    break
                await asyncio.sleep(0.01)
            await engine.cancel("run-1")
            events = await asyncio.wait_for(consumption, 3)
            assert events[-1][1]["status"] == "cancelled"
            assert not engine._tasks
            assert not engine.tool_runtime._sinks
            assert (await engine.execution_state("thread", str(tmp_path)))["resumable"]
    asyncio.run(scenario())
