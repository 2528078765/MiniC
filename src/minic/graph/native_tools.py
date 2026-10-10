"""LangChain 参数校验和 LangGraph ToolNode，业务权限仍由 ToolRuntime 执行。"""
from __future__ import annotations

import re
from typing import Any
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolNode
from langgraph.config import get_stream_writer
from langgraph.func import task
from pydantic import ConfigDict, create_model
from minic.graph.tools import list_tools

# 与执行器的可选参数保持一致。MCP 使用服务端原始 JSON Schema。
OPTIONAL = {
    "Read": {"start_line": 1, "end_line": None},
    "ReadMemory": {"topic": ""}, "GitDiff": {"path": ""},
    "GitLog": {"n": 10}, "IngestDirectory": {"path": ""},
    "DelegateToSubagent": {"allowed_tools": None, "budget": 4},
}

def normalized(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)

def native_tools(runtime: Any = None, state: dict | None = None, allowed: list[str] | None = None):
    state = state or {}
    tools = []
    seen = set()
    for spec in list_tools():
        if allowed is not None and spec.name not in allowed:
            continue
        name = normalized(spec.name)
        if name in seen:
            raise ValueError(f"工具名称冲突: {spec.name}")
        seen.add(name)
        fields = {}
        for key, kind in spec.args_schema.items():
            kind = kind if isinstance(kind, type) else Any
            default = OPTIONAL.get(spec.name, {}).get(key, ...)
            fields[key] = (kind | None if default is None and kind is not Any else kind, default)
        schema = getattr(spec, "json_schema", None) or create_model(
            f"{name}Args", __config__=ConfigDict(extra="forbid"), **fields
        )
        def make_execute(tool_name):
            async def execute(**args):
                args = {key: value for key, value in args.items() if value is not None}
                if runtime is None:
                    raise RuntimeError("工具执行核心不可用")
                writer = get_stream_writer()
                writer({"message": f"正在执行 {tool_name}", "tool": tool_name, "subagent_id": state.get("subagent_id")})
                result = await runtime.execute(
                    thread_id=state.get("thread_id", ""), run_id=state.get("run_id", ""),
                    tool=tool_name, args=args, tool_call_id=state.get("native_call_id"),
                    project_root=state.get("project_root"), subagent_id=state.get("subagent_id"),
                    native_graph=True,
                )
                return str(result.get("output") or result.get("status") or "")
            return execute
        tools.append(StructuredTool(name=name, description=spec.description or spec.name,
                                    args_schema=schema, coroutine=make_execute(spec.name)))
    return tools

async def execute_calls(state: dict, runtime: Any, allowed: list[str] | None = None) -> dict:
    last = (state.get("messages") or [None])[-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {}
    outputs = []
    context_results = list(state.get("tool_context", []))
    @task(name="execute_tool_call")
    async def execute_one(call, context):
        node = ToolNode(native_tools(runtime, context, allowed), handle_tool_errors=True)
        return await node.ainvoke({"messages": [AIMessage(content="", tool_calls=[call])]})
    # 串行保证审批顺序，稳定调用 ID + 持久化结果防止节点重放造成重复副作用。
    for call in last.tool_calls:
        context = {**state, "native_call_id": call["id"]}
        result = await execute_one(call, context)
        for message in result["messages"]:
            message.id = f"tool-result:{state.get('run_id')}:{call['id']}"
            outputs.append(message)
            runtime_id = f"{state['subagent_id']}:{call['id']}" if state.get("subagent_id") else call["id"]
            record = runtime._existing_result(state.get("run_id", ""), runtime_id) or {}
            context_results.append({"tool": call["name"], "args": call["args"], "tool_call_id": call["id"],
                                    "status": record.get("status", "failed" if message.status == "error" else "success"),
                                    "output": message.content, "backup_id": record.get("backup_id")})
    return {"messages": outputs, "tool_context": context_results, "tool_loop_count": int(state.get("tool_loop_count", 0)) + 1}
