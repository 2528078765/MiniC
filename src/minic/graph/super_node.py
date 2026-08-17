"""总图路由节点。"""  # 模块说明：总图意图路由

from __future__ import annotations  # 延迟注解求值

from typing import Any  # 任意类型

from langchain_core.messages import HumanMessage, SystemMessage  # 消息类型

from minic.graph.common import merged_memory_text, parse_model_json, is_knowledge_overview, render_document_lines, mcp_tools_text  # 记忆注入与 JSON 解析
from minic.graph.state import ChatState  # 图状态
from minic.middleware import retry_async  # 网络错误重试
from minic.rag.store import RagStore  # RAG 存储
from minic.skills import skill_inject_text  # SKILL 注入文本


async def route_node(  # 总图路由节点
    state: ChatState,  # 图状态
    chat_model: Any,  # 聊天模型
    rag_store: RagStore | None = None,  # RAG 存储（单库），可为空
    memory_store: Any = None,  # 长期记忆存储
    settings: Any = None,  # 应用配置
    skill_manager: Any = None,  # SKILL 管理器
) -> dict[str, Any]:  # 返回意图与文档清单
    """总图意图路由：结合知识库文档清单、长期记忆和用户输入分类。"""  # 函数说明
    documents = state.get("documents")
    if not documents and rag_store is not None:  # 单库文档清单
        documents = rag_store.inventory()
    if not documents:  # 无状态注入且无存储时给出空清单
        documents = []
    memory_text = merged_memory_text(state, memory_store, settings)  # 读取合并记忆
    skill_text = skill_inject_text(skill_manager)  # 读取启用的 SKILL 注入文本
    doc_lines = render_document_lines(documents) or "（暂无文档）"  # 渲染文档清单，空库给占位
    system_parts = [  # 系统提示片段
        (  # 意图分类说明
            "你是 MiniC 总图意图路由器。根据用户消息、会话历史、长期记忆和知识库文档清单，"  # 角色定义
            "把用户消息分为：knowledge（询问外部知识或知识库文档内容）、"  # 知识意图
            "chat_action（用户陈述自己的个人信息、普通聊天、指令或动作，"  # 动作意图
            "即使句末带“记住”也属于 chat_action）、"  # 个人信息仍归动作
            "memory_query（用户询问自己的个人信息）。"  # 记忆查询意图
            "示例：用户说“我的名字是张三”属于 chat_action；用户问“我叫什么”属于 memory_query；"  # 示例
            "用户问“什么是 LangGraph”属于 knowledge；用户问“你都知道什么/知识库里有什么”属于 knowledge。"  # 知识示例
            "只输出 JSON：{\"intent\": \"...\"}"  # 输出格式
        ),  # 提示结束
        f"知识库文档清单：\n{doc_lines}",  # 注入文档清单
    ]  # 提示列表结束
    mcp_text = mcp_tools_text(max_count=10, max_desc=60)  # 已连接 MCP 工具（供路由判断外部能力类问题）
    if mcp_text:  # 有已连接 MCP 工具
        system_parts.append(f"已连接的 MCP 工具：\n{mcp_text}")  # 注入 MCP 工具清单
    if memory_text:  # 有记忆时
        system_parts.append(f"长期记忆：\n{memory_text}")  # 注入长期记忆
    if skill_text:  # 有启用的 SKILL 时
        system_parts.append(f"当前启用的 SKILL 能力：\n{skill_text}")  # 注入 SKILL 描述
    messages = [  # 组装模型消息
        SystemMessage(content="\n\n".join(system_parts)),  # 系统提示
        HumanMessage(content=state["user_message"]),  # 用户消息
    ]  # 消息结束
    response = await retry_async(lambda: chat_model.ainvoke(messages), max_retries=2)  # 调用模型并重试网络错误
    try:  # 解析意图
        data = parse_model_json(response.content)  # 解析 JSON
        intent = str(data.get("intent", "knowledge"))  # 模型输出无效时默认按知识问题处理
    except (ValueError, TypeError, AttributeError):  # 解析失败
        intent = "knowledge"  # 回退知识意图
    if intent in {"user_info", "other"}:  # 兼容旧意图名
        intent = "chat_action"  # 统一归入动作子图
    if intent not in {"knowledge", "chat_action", "memory_query"}:  # 过滤非法意图
        intent = "knowledge"  # 默认知识
    if is_knowledge_overview(state["user_message"]):  # 概况类问题硬兜底：模型判错也走知识库子图
        intent = "knowledge"  # 由知识库子图直接回答文档清单（不做检索）
    return {"intent": intent, "documents": documents}  # 返回意图并保留文档清单给后续子图
