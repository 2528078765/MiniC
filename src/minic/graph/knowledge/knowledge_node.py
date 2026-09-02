"""知识库子图节点。"""  # 模块说明：知识库问答节点

from __future__ import annotations  # 延迟注解求值

from typing import Any  # 任意类型

from langchain_core.messages import HumanMessage, SystemMessage  # 消息类型

from minic.graph.common import merged_memory_text, stream_answer, is_knowledge_overview, append_documents_block, append_mcp_block, render_document_lines  # 记忆注入与流式回答
from minic.graph.state import ChatState  # 图状态
from minic.middleware import retry_async  # 网络错误重试
from minic.rag.store import RagStore  # RAG 存储
from minic.skills import skill_inject_text  # SKILL 注入文本


async def rewrite_query_node(  # 改写检索问题
    state: ChatState,  # 图状态
    rag_store: RagStore,  # RAG 存储
    chat_model: Any,  # 聊天模型
) -> dict[str, str]:  # 返回改写后的查询
    """根据历史改写问题；无历史时直接使用原始问题。"""  # 函数说明
    del rag_store  # 本节点不需要 RAG 存储
    history = state.get("history", [])  # 读取会话历史
    user_message = state["user_message"]  # 读取用户消息
    if len(history) <= 1:  # 没有可用的追问历史
        return {"rewritten_query": user_message}  # 直接使用原始问题
    messages = [  # 组装改写消息
        SystemMessage(  # 改写指令 + few-shot 示例
            content=(  # 提示内容
                "把用户最新问题改写成独立检索问题：补全代词指代与省略的背景，只输出改写后的文本。\n"  # 指令
                "示例：上文在讨论 LangChain 配置，用户问\"那它的代理呢\" → LangChain 的代理如何配置\n"  # 代词消解示例
                "示例：上文在讨论部署，用户问\"端口呢\" → 该服务的部署端口是什么"  # 省略背景示例
            )  # 内容结束
        ),  # 系统提示结束
        *[  # 追加最近历史
            HumanMessage(content=f"{message.get('role')}: {message.get('content')}")  # 历史消息
            for message in history[-8:]  # 最多取最近 8 条
        ],  # 历史结束
    ]  # 消息结束
    response = await retry_async(lambda: chat_model.ainvoke(messages), max_retries=2)  # 调用模型并重试
    rewritten = response.content.strip()  # 清理改写结果
    return {"rewritten_query": rewritten or user_message}  # 改写为空时回退原始问题


async def retrieve_node(  # 混合检索
    state: ChatState,  # 图状态
    rag_store: RagStore,  # RAG 存储（单库）
) -> dict[str, list[dict[str, Any]]]:  # 返回上下文与来源
    """执行混合检索并整理来源（单库）。"""  # 函数说明
    if is_knowledge_overview(state["user_message"]):  # 概况类问题（"你都知道什么"）不做检索
        return {"contexts": [], "sources": []}  # 由回答节点直接给出文档清单
    top_k = rag_store.settings.rag.top_k  # 读取配置的返回条数
    results = rag_store.query(  # 混合检索（向量 + BM25）
        state["rewritten_query"],  # 使用改写后的查询
        top_k=top_k,  # 读取配置的返回条数
    )  # 查询结束
    contexts = [result.to_dict() for result in results]  # 转成上下文字典
    sources = []  # 来源列表
    seen = set()  # 去重集合
    for context in contexts:  # 遍历上下文
        key = (context["file_path"], context["section"])  # 按文件+章节去重来源
        if key in seen:  # 已出现过
            continue  # 跳过
        seen.add(key)  # 记录来源
        sources.append(  # 追加来源
            {  # 来源字段
                "file_path": context["file_path"],  # 文件路径
                "section": context["section"],  # 章节
                "title": context["title"],  # 标题
                "score": context["score"],  # 相关分数
            }  # 来源结束
        )  # 追加结束
    return {"contexts": contexts, "sources": sources}  # 返回上下文与来源


async def knowledge_answer_node(  # 知识库回答节点
    state: ChatState,  # 图状态
    chat_model: Any,  # 聊天模型
    memory_store: Any,  # 长期记忆存储
    settings: Any,  # 应用配置
    skill_manager: Any = None,  # SKILL 管理器
    rag_store: RagStore | None = None,  # RAG 存储（回答节点兜底取文档清单）
) -> dict[str, str]:  # 返回回答
    """生成知识库回答提示词并流式执行。"""  # 函数说明
    memory_text = merged_memory_text(state, memory_store, settings)  # 读取合并记忆
    skill_text = skill_inject_text(skill_manager)  # 读取启用的 SKILL 注入文本
    contexts = state.get("contexts", [])  # 读取检索上下文
    documents = state.get("documents") or []  # 读取文档清单（路由节点已注入）
    if not documents and rag_store is not None:  # 状态缺失时兜底从存储读取
        documents = rag_store.inventory()  # 实时文档清单
    if contexts:  # 有资料
        context_block = "\n\n".join(  # 拼接资料块
            f"[{index + 1}] 来源：{context['file_path']} 章节：{context['section']}\n{context['text']}"  # 每条资料
            for index, context in enumerate(contexts)  # 编号资料
        )  # 资料结束
        parts = [  # 系统提示片段
            "你是 MiniC 知识助手。请只依据下面的资料回答，不要编造。",  # 角色约束
            "直接输出纯文本答案，不要使用 JSON 包装，不要输出代码块。",  # 输出约束
            "回答时可用 [编号] 标注引用来源（如 [1]、[2]）。",  # 引用标注
        ]  # 提示结束
        if memory_text:  # 有长期记忆
            parts.append(f"长期记忆：\n{memory_text}")  # 注入记忆
        if skill_text:  # 有启用的 SKILL
            parts.append(f"当前启用的 SKILL 能力：\n{skill_text}")  # 注入 SKILL 描述
        parts.append(f"资料：\n{context_block}")  # 注入资料
        system_text = "\n\n".join(parts)  # 组合系统提示
        system_text = append_documents_block(system_text, documents)  # 常驻文档清单（概况类改述也能按清单回答）
    elif is_knowledge_overview(state["user_message"]):  # 概况类问题：直接回答文档清单
        doc_lines = render_document_lines(documents)  # 渲染清单
        if doc_lines:  # 有文档
            system_text = (  # 提示按清单回答
                "你是 MiniC 知识助手。用户在询问知识库概况，请直接根据下面的知识库文档清单回答，"
                "列出文档名即可，不要编造清单外的内容，不要输出代码块。\n\n"
                f"知识库文档清单：\n{doc_lines}"
            )
        else:  # 空库
            system_text = "你是 MiniC 知识助手。知识库中暂无文档。不要使用 JSON 包装。"  # 明确告知空库
    else:  # 检索无结果
        system_text = "你是 MiniC 知识助手。没有检索到相关资料，请直接回复：未检索到。不要使用 JSON 包装。"  # 无资料时明确回答未检索到
        if skill_text:  # 有启用的 SKILL
            system_text += f"\n\n当前启用的 SKILL 能力：\n{skill_text}"  # 注入 SKILL 描述
    system_text = append_mcp_block(system_text)  # 常驻已连接 MCP 工具清单（问 MCP 能力时按真实连接回答）
    answer = await stream_answer(chat_model, system_text, state.get("history", []), state["user_message"])  # 流式生成回答
    return {"answer": answer}  # 返回回答
