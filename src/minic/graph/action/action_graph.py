"""动作子图编译：教科书式 ReAct（图上回边）。"""

from __future__ import annotations  # 延迟注解求值

from typing import Any  # 任意类型

from langgraph.graph import END, StateGraph  # LangGraph 图组件

from minic.graph.action.action_node import (  # 动作节点逻辑
    action_answer_node,  # 回答节点
    agent_node,  # ReAct 决策节点
    extract_memory_node,  # 记忆提取节点
    should_continue,  # 条件边
    tools_node,  # ReAct 工具执行节点
)  # 导入结束
from minic.graph.state import ChatState  # 图状态
from minic.memory import LongTermMemoryStore  # 长期记忆存储
from minic.rag.store import RagStore  # RAG 存储


def build_action_graph(  # 构建动作子图
    rag_store: RagStore,  # RAG 存储（本子图暂不使用）
    chat_model: Any,  # 聊天模型
    memory_store: LongTermMemoryStore | None = None,  # 长期记忆存储
    settings: Any = None,  # 应用配置
    tool_runtime: Any = None,  # 共享工具执行核心
    skill_manager: Any = None,  # SKILL 管理器
):  # 返回编译后的子图
    """动作子图：记忆提取 -> agent <-> tools -> 回答（教科书式 ReAct 回边）。"""  # 函数说明
    del rag_store  # 动作子图不需要 RAG 存储
    async def extract_node(state: ChatState) -> dict[str, Any]:  # 记忆提取节点闭包
        """记忆提取节点。"""  # 节点说明
        return await extract_memory_node(state, chat_model, memory_store)  # 绑定依赖后调用提取逻辑

    async def agent_node_call(state: ChatState) -> dict[str, Any]:  # agent 节点闭包
        """ReAct 决策节点。"""  # 节点说明
        return await agent_node(state, chat_model, memory_store, settings, skill_manager)  # 绑定依赖后调用

    async def tools_node_call(state: ChatState) -> dict[str, Any]:  # tools 节点闭包
        """ReAct 工具执行节点。"""  # 节点说明
        return await tools_node(state, tool_runtime)  # 绑定依赖后调用

    async def answer_node_call(state: ChatState) -> dict[str, str]:  # 回答节点闭包
        """生成回答节点。"""  # 节点说明
        return await action_answer_node(state, chat_model, memory_store, settings, skill_manager)  # 绑定依赖后调用回答逻辑

    builder = StateGraph(ChatState)  # 创建子图构建器
    builder.add_node("extract_memory", extract_node)  # 注册记忆提取节点
    builder.add_node("agent", agent_node_call)  # 注册决策节点
    builder.add_node("tools", tools_node_call)  # 注册工具执行节点
    builder.add_node("answer", answer_node_call)  # 注册回答节点
    builder.set_entry_point("extract_memory")  # 设置入口节点
    builder.add_conditional_edges(  # 按意图分流：动作类进 ReAct 循环，其余直接回答
        "extract_memory",  # 记忆提取节点
        lambda state: "agent" if state.get("intent", "chat_action") == "chat_action" else "answer",  # 意图判断
        {"agent": "agent", "answer": "answer"},  # 分流映射
    )  # 分流结束
    builder.add_conditional_edges("agent", should_continue, {"tools": "tools", "answer": "answer"})  # 决策后分流
    builder.add_edge("tools", "agent")  # 工具结果回传 agent（教科书式回边）
    builder.add_edge("answer", END)  # 回答后结束
    return builder.compile()  # 编译并返回子图
