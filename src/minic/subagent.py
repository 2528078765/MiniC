"""子 Agent 使用独立 LangGraph 消息状态和原生工具调用。"""
from __future__ import annotations
import asyncio
import uuid
import json
from pathlib import Path
from datetime import datetime
from typing import Any
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.types import TimeoutPolicy, Command
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import NodeTimeoutError, GraphBubbleUp
from minic.graph.state import ChatState
from minic.graph.native_tools import native_tools, execute_calls
from minic.graph.common import merged_memory_text

class SubAgentManager:
    def __init__(self, chat_model, memory_store, tool_runtime, settings, workspace):
        self.chat_model, self.memory_store, self.tool_runtime = chat_model, memory_store, tool_runtime
        self.settings, self.workspace = settings, str(workspace)
        self.semaphore = asyncio.Semaphore(settings.subagent.max_concurrent)
        self._recent = []
        self.checkpointer = InMemorySaver()
        self.index_path = Path(workspace) / ".minic" / "memory" / "subagents.json"
        try:
            saved = json.loads(self.index_path.read_text(encoding="utf-8"))
            self._recent = saved.get("recent", [])
            self._specs = saved.get("tasks", {})
        except (OSError, ValueError):
            self._specs = {}

    def status(self):
        return list(self._recent)

    def _record(self, sid, status, output):
        self._recent = [item for item in self._recent if item["subagent_id"] != sid]
        self._recent.append({"subagent_id": sid, "status": status, "output": output,
                             "output_summary": output[:120], "created_at": datetime.now().astimezone().isoformat()})
        self._recent = self._recent[-20:]
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.index_path.with_suffix(".tmp")
        temp.write_text(json.dumps({"recent": self._recent, "tasks": self._specs}, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.index_path)

    async def resume(self, sid, approval_id, decision):
        spec = self._specs.get(sid)
        if spec is None:
            raise KeyError("子任务不存在或不是独立任务")
        return await self.run(**spec, subagent_id=sid, resume_decision=(approval_id, decision))

    async def run(self, task, allowed_tools=None, budget=4, thread_id=None, emit=None,
                  subagent_id=None, project_root=None, native_graph=False, run_id=None, resume_decision=None):
        sid = subagent_id or str(uuid.uuid4())
        budget = min(max(int(budget or 4), 1), 20)
        # 禁止无限递归委托；白名单为空时不给任何工具。
        from minic.graph.tools import list_tools
        allowed = ([s.name for s in list_tools() if s.name != "DelegateToSubagent"]
                   if allowed_tools is None else [name for name in allowed_tools if name != "DelegateToSubagent"])
        project_root = project_root or self.workspace
        owner = thread_id or sid
        if not native_graph:
            self._specs[sid] = {"task": task, "allowed_tools": allowed_tools, "budget": budget,
                                "thread_id": owner, "project_root": project_root, "run_id": run_id}
            self._record(sid, "running", "")
        async def agent(state):
            memory = merged_memory_text({"project_root": project_root}, self.memory_store, self.settings)
            prompt = f"你是 MiniC 子 Agent，只完成指定子任务。长期记忆只读。\n{memory}"
            tools = native_tools(allowed=allowed)
            model = self.chat_model.bind_tools(tools) if tools and int(state.get("tool_loop_count", 0)) < budget else self.chat_model
            response = await model.ainvoke([SystemMessage(content=prompt), *state.get("messages", [])])
            return {"messages": [response]}
        async def tools(state):
            state = {**state, "thread_id": owner, "run_id": run_id or sid,
                     "project_root": project_root, "subagent_id": sid}
            return await execute_calls(state, self.tool_runtime, allowed)
        def route(state):
            last = state["messages"][-1]
            return "tools" if isinstance(last, AIMessage) and last.tool_calls and int(state.get("tool_loop_count", 0)) < budget else END
        builder = StateGraph(ChatState)
        builder.add_node("agent", agent, timeout=TimeoutPolicy(run_timeout=self.settings.subagent.timeout_seconds))
        builder.add_node("tools", tools)
        builder.set_entry_point("agent")
        builder.add_conditional_edges("agent", route, {"tools": "tools", END: END})
        builder.add_edge("tools", "agent")
        graph = builder.compile(checkpointer=None if native_graph else self.checkpointer)
        config = None if native_graph else {"configurable": {"thread_id": f"subagent:{sid}"}, "recursion_limit": 50}
        if not native_graph:
            self.tool_runtime.attach(owner, emit or (lambda *_: None))
        try:
            graph_input = {"messages": [HumanMessage(content=task, id=f"task:{sid}")], "tool_loop_count": 0}
            if resume_decision:
                snapshot = await graph.aget_state(config, subgraphs=True)
                approval_id, decision = resume_decision
                item = next((item for item in snapshot.interrupts if item.value.get("id") == approval_id), None)
                if item is None:
                    raise KeyError("审批不存在或已处理")
                if decision not in item.value["options"]:
                    raise ValueError("不支持的审批决定")
                graph_input = Command(resume={item.id: decision})
            async with self.semaphore:
                result = await graph.ainvoke(graph_input, config=config, durability="sync")
            last = result["messages"][-1]
            if result.get("__interrupt__"):
                pending = [{**item.value, "interrupt_id": item.id} for item in result["__interrupt__"]]
                self._record(sid, "waiting_approval", "等待审批")
                return {"subagent_id": sid, "status": "waiting_approval", "output": "等待审批", "pending_approvals": pending}
            completed = isinstance(last, AIMessage) and not last.tool_calls
            output = str(last.content or "") if completed else "子任务已达到工具预算"
            status = "completed" if completed else "failed"
            self._record(sid, status, output)
            return {"subagent_id": sid, "status": status, "output": output}
        except NodeTimeoutError:
            self._record(sid, "failed", "子任务超时")
            return {"subagent_id": sid, "status": "failed", "output": "子任务超时"}
        finally:
            if not native_graph:
                self.tool_runtime.detach(owner)
