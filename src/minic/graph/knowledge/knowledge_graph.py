"""知识库子图编译。"""  # 模块说明：组装知识库子图

from __future__ import annotations  # 延迟注解求值

from typing import Any  # 任意类型

from langgraph.graph import END, StateGraph  # LangGraph 图组件

from minic.graph.knowledge.knowledge_node import (  # 导入节点逻辑
    knowledge_answer_node,  # 回答节点
    retrieve_node,  # 检索节点
    rewrite_query_node,  # 改写节点
)  # 导入结束
from minic.graph.state import ChatState  # 图状态
from minic.memory import LongTermMemoryStore  # 长期记忆存储
from minic.rag.store import RagStore  # RAG 存储


def build_knowledge_graph(  # 构建知识库子图
    rag_store: RagStore,  # RAG 存储（单库）
    chat_model: Any,  # 聊天模型
    memory_store: LongTermMemoryStore | None = None,  # 长期记忆存储
    settings: Any = None,  # 应用配置
    skill_manager: Any = None,  # SKILL 管理器
):  # 返回编译后的子图
    """知识库子图：改写 -> 检索 -> 回答。"""  # 函数说明
    async def rewrite_node(state: ChatState) -> dict[str, str]:  # 改写查询节点闭包
        """改写查询节点。"""  # 节点说明
        return await rewrite_query_node(state, rag_store, chat_model)  # 绑定依赖后调用改写逻辑

    async def retrieve_node_call(state: ChatState) -> dict[str, list[dict[str, Any]]]:  # 检索节点闭包
        """混合检索节点（单库）。"""  # 节点说明
        return await retrieve_node(state, rag_store)  # 绑定 RAG 存储后调用检索逻辑

    async def answer_node_call(state: ChatState) -> dict[str, str]:  # 回答节点闭包
        """生成回答节点。"""  # 节点说明
        return await knowledge_answer_node(state, chat_model, memory_store, settings, skill_manager, rag_store)  # 绑定依赖后调用回答逻辑

    builder = StateGraph(ChatState)  # 创建子图构建器
    builder.add_node("rewrite_query", rewrite_node)  # 注册改写节点
    builder.add_node("retrieve", retrieve_node_call)  # 注册检索节点
    builder.add_node("answer", answer_node_call)  # 注册回答节点
    builder.set_entry_point("rewrite_query")  # 设置入口节点
    builder.add_edge("rewrite_query", "retrieve")  # 改写后进入检索
    builder.add_edge("retrieve", "answer")  # 检索后进入回答
    builder.add_edge("answer", END)  # 回答后结束
    return builder.compile()  # 编译并返回子图
