"""聊天流式执行引擎。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from minic.chat.memory import ShortMemoryStore
from minic.graph.common import split_incomplete_dsml, strip_dsml
from minic.rag.store import RagStore


def _estimate_tokens(chars: int) -> int:
    """字符数估算 token（中文约 1 字/token、英文约 4 字符/token，取折中 0.6）。"""
    return max(int(chars * 0.6), 1)


def _record_usage(message: str, history: list[dict[str, Any]], answer: str, thread_id: str) -> None:
    """记录一轮对话的 token 消耗到 ~/.minic/usage.jsonl（总量统计用）。"""
    prompt_chars = len(message) + sum(len(str(item.get("content", ""))) for item in history)
    completion_chars = len(answer)
    record = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "thread_id": thread_id,
        "prompt_tokens": _estimate_tokens(prompt_chars),
        "completion_tokens": _estimate_tokens(completion_chars),
    }
    path = Path.home() / ".minic" / "usage.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


class ChatEngine:
    """原生图状态负责执行，JSON 会话文件仅作为桌面展示和导出副本。"""
    def __init__(self, rag_store, short_memory, graph, tool_runtime=None, summarize_history=None):
        self.rag_store, self.short_memory, self.graph = rag_store, short_memory, graph
        self.tool_runtime, self.summarize_history = tool_runtime, summarize_history
        self._tasks = {}
        self._controls = {}
        self._queues = {}
        self.decision_path = short_memory.root_dir.parent / "approval-decisions.json"

    def config(self, thread_id, workspace):
        import hashlib
        key = hashlib.sha256(f"{Path(workspace).resolve()}:{thread_id}".encode()).hexdigest()
        return {"configurable": {"thread_id": key}, "recursion_limit": 80}

    async def state(self, thread_id, workspace):
        return await self.graph.aget_state(self.config(thread_id, workspace), subgraphs=True)

    def _interrupts(self, snapshot):
        found = {item.id: item for item in getattr(snapshot, "interrupts", ())}
        for task in snapshot.tasks:
            for item in task.interrupts:
                found[item.id] = item
            if getattr(task.state, "tasks", None):
                for item in self._interrupts(task.state):
                    found[item.id] = item
        return list(found.values())

    async def execution_state(self, thread_id, workspace):
        snap = await self.state(thread_id, workspace)
        decisions = self._decisions()
        pending = [{**item.value, "interrupt_id": item.id, "decision": decisions.get(item.id)}
                   for item in self._interrupts(snap) if isinstance(item.value, dict)]
        return {"thread_id": thread_id, "resumable": bool(snap.next),
                "status": "waiting_approval" if pending else "interrupted" if snap.next else "completed",
                "run_id": snap.values.get("run_id"), "message_id": snap.values.get("message_id"),
                "pending_approvals": pending, "next": list(snap.next)}

    def _decisions(self):
        try:
            return json.loads(self.decision_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    async def approve(self, thread_id, workspace, approval_id, decision):
        snap = await self.state(thread_id, workspace)
        item = next((i for i in self._interrupts(snap) if isinstance(i.value, dict) and i.value.get("id") == approval_id), None)
        if item is None:
            raise KeyError("审批不存在或已处理")
        if decision not in item.value.get("options", []):
            raise ValueError("不支持的审批决定")
        decisions = self._decisions()
        if item.id in decisions:
            raise ValueError("审批已处理")
        decisions[item.id] = decision
        self.decision_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.decision_path.with_suffix(".tmp")
        temp.write_text(json.dumps(decisions, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.decision_path)

    def _inbox(self):
        """暂停时的补充输入也使用原生检查点，独立于仍在等待审批的执行图。"""
        from langgraph.graph import StateGraph, MessagesState, START, END
        builder = StateGraph(MessagesState)
        builder.add_node("receive", lambda state: {})
        builder.add_edge(START, "receive")
        builder.add_edge("receive", END)
        return builder.compile(checkpointer=self.graph.checkpointer)

    def _inbox_config(self, thread_id, workspace):
        config = self.config(thread_id, workspace)
        config["configurable"]["thread_id"] += ":continuations"
        return config

    async def cancel(self, run_id):
        task = self._tasks.get(run_id)
        control = self._controls.get(run_id)
        if control:
            control.request_drain("cancelled")
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Task.cancel 可在协程第一次运行之前生效，此时其 except 分支不会执行。
            queue = self._queues.get(run_id)
            if queue:
                queue.put_nowait(("cancelled",))

    async def close(self):
        await asyncio.gather(*(self.cancel(run_id) for run_id in list(self._tasks)))

    async def _run_graph(self, queue, graph_input, config, run_id):
        try:
            async for namespace, mode, payload in self.graph.astream(
                graph_input, config=config, stream_mode=["messages", "values", "updates", "custom"],
                subgraphs=True, durability="sync", control=self._controls[run_id],
            ):
                queue.put_nowait(("graph", mode, payload, list(namespace)))
            queue.put_nowait(("graph_end",))
        except asyncio.CancelledError:
            queue.put_nowait(("cancelled",))
            raise
        except Exception as exc:
            queue.put_nowait(("graph_error", exc))

    def _legacy_messages(self, history):
        from langchain_core.messages import AIMessage, HumanMessage
        messages = []
        # 旧记录没有完整 reasoning metadata；只把已完成的工具结果转换为普通历史说明。
        for index, entry in enumerate(history):
            content = str(entry.get("content") or "")
            if entry.get("role") == "tool":
                content = "此前工具执行结果：" + content
            elif entry.get("tool_calls"):
                continue
            if not content:
                continue
            cls = HumanMessage if entry.get("role") in ("human", "user") else AIMessage
            messages.append(cls(content=content, id=f"legacy:{index}"))
        return messages

    def _projection(self, messages):
        from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
        projection = []
        for message in messages:
            role = "human" if isinstance(message, HumanMessage) else "tool" if isinstance(message, ToolMessage) else "ai"
            entry = {"role": role, "content": message.content, "message_id": message.id}
            if isinstance(message, AIMessage) and message.tool_calls:
                entry["tool_calls"] = message.tool_calls
            if isinstance(message, ToolMessage):
                entry["tool_call_id"] = message.tool_call_id
            projection.append(entry)
        return projection

    def _project(self, thread_id, workspace, messages):
        """从权威原生状态重建展示副本，避免工具多次重放污染历史。"""
        thread = self.short_memory.load(thread_id, workspace) or {}
        projection = self._projection(messages)
        thread["messages"] = projection
        thread["updated_at"] = datetime.now().astimezone().isoformat()
        path = self.short_memory._locate(thread_id, workspace)
        if path:
            self.short_memory._write(thread, path)

    async def stream_chat(self, thread_id, workspace, project_root, message, run_id, message_id, resume=False):
        from langchain_core.messages import HumanMessage, AIMessageChunk
        from langgraph.types import Command
        config = self.config(thread_id, workspace)
        snapshot = await self.state(thread_id, workspace)
        thread = self.short_memory.load(thread_id, workspace) or {}
        history = thread.get("messages", [])
        resume = resume or bool(snapshot.next)
        if resume:
            if not snapshot.next:
                raise ValueError("该会话没有可恢复的任务")
            interrupts = self._interrupts(snapshot)
            decisions = self._decisions()
            inbox = self._inbox()
            inbox_config = self._inbox_config(thread_id, workspace)
            if message.strip():
                await inbox.ainvoke({"messages": [HumanMessage(content=message, id=f"continuation:{uuid.uuid4()}")]}, inbox_config)
            pending_input = await inbox.aget_state(inbox_config)
            config["configurable"]["continuation_messages"] = pending_input.values.get("messages", [])
            if any(item.id not in decisions for item in interrupts):
                yield ("message_start", {"thread_id": thread_id, "message_id": message_id, "run_id": run_id, "resumed": True})
                for item in interrupts:
                    if item.id not in decisions:
                        yield ("approval_requested", {**item.value, "interrupt_id": item.id})
                yield ("message_end", {"message_id": message_id, "status": "waiting_approval"})
                yield ("done", {"thread_id": thread_id, "status": "waiting_approval", "sources": []})
                return
            graph_input = Command(resume={item.id: decisions[item.id] for item in interrupts}) if interrupts else None
        else:
            if snapshot.next:
                yield ("error", {"code": "PENDING_RUN", "message": "该会话有未完成任务，请先恢复", "detail": {}})
                yield ("done", {"thread_id": thread_id, "status": "interrupted", "sources": []})
                return
            stored = snapshot.values.get("messages", [])
            native = [] if stored else self._legacy_messages(history)
            if stored and history != self._projection(stored):
                # 压缩/备份恢复主动修改了会话展示文件，同步一次原生状态。
                from langchain_core.messages import RemoveMessage
                from langgraph.graph.message import REMOVE_ALL_MESSAGES
                native = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *self._legacy_messages(history)]
            native.append(HumanMessage(content=message, id=f"human:{message_id}"))
            graph_input = {"thread_id": thread_id, "workspace": workspace, "project_root": project_root,
                           "user_message": message, "history": history + [{"role": "human", "content": message}],
                           "messages": native, "run_id": run_id, "message_id": message_id,
                           "answer": "", "sources": [], "contexts": [], "tool_context": [], "tool_loop_count": 0,
                           "confirmation": "", "extracted_topics": []}
            if stored and history != self._projection(stored):
                graph_input.update(context_summary="", summary_until_id="")
        yield ("message_start", {"thread_id": thread_id, "message_id": message_id, "run_id": run_id, "resumed": resume})
        queue = asyncio.Queue()
        self._queues[run_id] = queue
        if self.tool_runtime:
            self.tool_runtime.attach(thread_id, lambda name, data: queue.put_nowait(("tool", name, data)))
        from langgraph.runtime import RunControl
        self._controls[run_id] = RunControl()
        task = asyncio.create_task(self._run_graph(queue, graph_input, config, run_id))
        self._tasks[run_id] = task
        answer, sources, dsml_buffer = "", [], ""
        try:
            while True:
                item = await queue.get()
                if item[0] == "cancelled":
                    yield ("message_end", {"message_id": message_id, "status": "cancelled"})
                    yield ("done", {"thread_id": thread_id, "status": "cancelled", "sources": []})
                    return
                if item[0] == "graph_end":
                    break
                if item[0] == "graph_error":
                    raise item[1]
                if item[0] == "tool":
                    # 审批由已保存的 interrupt 发出，防止事件先到达而检查点尚未保存。
                    if item[1] != "approval_requested":
                        yield (item[1], item[2])
                    continue
                mode, payload, namespace = item[1:]
                if mode == "messages":
                    chunk, metadata = payload
                    node = metadata.get("langgraph_node")
                    if node not in ("answer", "agent") or not isinstance(chunk, AIMessageChunk) or chunk.tool_calls:
                        continue
                    delta = chunk.content
                    if isinstance(delta, str) and delta:
                        dsml_buffer, clean = split_incomplete_dsml(dsml_buffer + delta)
                        if clean:
                            yield ("token", {"message_id": message_id, "delta": clean,
                                             "channel": "activity" if node == "agent" else "answer", "namespace": namespace})
                elif mode == "updates":
                    for node in payload:
                        if not node.startswith("__"):
                            yield ("graph_step", {"node": node, "status": "completed", "namespace": namespace})
                elif mode == "custom":
                    yield ("graph_progress", {"progress": payload, "namespace": namespace})
                elif mode == "values" and not namespace:
                    answer, sources = payload.get("answer", ""), payload.get("sources", [])
            await task
            snapshot = await self.state(thread_id, workspace)
            self._project(thread_id, workspace, snapshot.values.get("messages", []))
            if resume:
                from langchain_core.messages import RemoveMessage
                consumed = {m.id for m in snapshot.values.get("messages", [])}
                clear = [RemoveMessage(id=m.id) for m in config["configurable"]["continuation_messages"] if m.id in consumed]
                if clear:
                    await inbox.ainvoke({"messages": clear}, inbox_config)
            pending = self._interrupts(snapshot)
            if pending:
                for interrupt in pending:
                    yield ("approval_requested", {**interrupt.value, "interrupt_id": interrupt.id})
                yield ("message_end", {"message_id": message_id, "status": "waiting_approval"})
                yield ("done", {"thread_id": thread_id, "status": "waiting_approval", "sources": []})
                return
            answer = strip_dsml(snapshot.values.get("answer", answer))
            _record_usage(message, history, answer, thread_id)
            yield ("answer", {"message_id": message_id, "text": answer})
            yield ("message_end", {"message_id": message_id, "status": "completed"})
            yield ("done", {"thread_id": thread_id, "status": "completed", "sources": sources})
        except Exception as exc:
            yield ("error", {"code": "INTERNAL_ERROR", "message": str(exc), "detail": {}})
            yield ("message_end", {"message_id": message_id, "status": "failed"})
            yield ("done", {"thread_id": thread_id, "status": "failed", "sources": []})
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._tasks.pop(run_id, None)
            self._controls.pop(run_id, None)
            self._queues.pop(run_id, None)
            if self.tool_runtime:
                self.tool_runtime.detach(thread_id)

def new_run_ids():
    return str(uuid.uuid4()), str(uuid.uuid4())
