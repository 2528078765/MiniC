"""总图编译与兼容入口。"""  # 模块说明：组装总图

from __future__ import annotations  # 延迟注解求值

from typing import Any  # 任意类型

from langgraph.checkpoint.memory import MemorySaver  # 轻量内存 checkpointer
from langgraph.graph import END, StateGraph  # LangGraph 图组件

from minic.graph.action.action_graph import build_action_graph  # 动作子图构建
from minic.graph.knowledge.knowledge_graph import build_knowledge_graph  # 知识子图构建
from minic.graph.state import ChatState  # 图状态
from minic.graph.super_node import route_node  # 总图路由
from minic.memory import LongTermMemoryStore  # 长期记忆存储
from minic.rag.store import RagStore  # RAG 存储


def build_super_graph(  # 构建总图
    rag_store: RagStore,  # RAG 存储（单库）
    chat_model: Any,  # 聊天模型
    memory_store: LongTermMemoryStore | None = None,  # 长期记忆存储
    settings: Any = None,  # 应用配置
    tool_runtime: Any = None,  # 共享工具执行核心
    skill_manager: Any = None,  # SKILL 管理器
):  # 返回编译后的总图
    """总图：路由 -> 知识库子图 / 动作子图。"""  # 函数说明
    async def route_node_call(state: ChatState) -> dict[str, Any]:  # 路由节点闭包
        """总图路由节点。"""  # 节点说明
        return await route_node(  # 绑定依赖后调用路由逻辑
            state, chat_model, rag_store, memory_store, settings, skill_manager
        )

    knowledge_subgraph = build_knowledge_graph(  # 构建知识子图
        rag_store, chat_model, memory_store, settings, skill_manager
    )
    action_subgraph = build_action_graph(rag_store, chat_model, memory_store, settings, tool_runtime, skill_manager)  # 构建动作子图

    builder = StateGraph(ChatState)  # 创建总图构建器
    builder.add_node("route", route_node_call)  # 注册路由节点
    builder.add_node("knowledge", knowledge_subgraph)  # 注册知识子图
    builder.add_node("action", action_subgraph)  # 注册动作子图
    builder.set_entry_point("route")  # 设置入口为路由
    builder.add_conditional_edges(  # 按意图分流
        "route",  # 路由节点
        lambda state: state.get("intent", "knowledge"),  # 读取意图
        {  # 分流映射
            "knowledge": "knowledge",  # 知识问题进知识子图
            "chat_action": "action",  # 动作进动作子图
            "memory_query": "action",  # 记忆查询进动作子图
        },  # 映射结束
    )  # 分流结束
    builder.add_edge("knowledge", END)  # 知识子图结束
    builder.add_edge("action", END)  # 动作子图结束
    return builder.compile(checkpointer=MemorySaver())  # 轻量 checkpointer：图状态内存中可恢复（规格书 4.1）


def build_chat_graph(  # 兼容旧入口
    rag_store: RagStore,  # RAG 存储（单库）
    chat_model: Any,  # 聊天模型
    memory_store: LongTermMemoryStore | None = None,  # 长期记忆存储
    settings: Any = None,  # 应用配置
    tool_runtime: Any = None,  # 共享工具执行核心
    skill_manager: Any = None,  # SKILL 管理器
):  # 返回编译后的总图
    """兼容旧入口，等价于总图 + 子图架构。"""  # 函数说明
    return build_super_graph(  # 直接复用总图构建
        rag_store, chat_model, memory_store, settings, tool_runtime, skill_manager
    )
