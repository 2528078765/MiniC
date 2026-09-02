"""图状态类型定义。"""  # 模块说明：定义图共享状态

from __future__ import annotations  # 延迟注解求值，兼容类型提示

from typing import Any, TypedDict  # 引入 TypedDict 与任意类型


class ChatState(TypedDict, total=False):  # 图状态类型，所有字段均可选
    """LangGraph 问答状态。"""  # 状态说明

    thread_id: str  # 当前会话 ID
    workspace: str  # 会话归属/展示用工作区
    project_root: str  # 项目数据落盘根目录
    user_message: str  # 用户本次输入
    history: list[dict[str, Any]]  # 会话历史消息
    messages: list[Any]  # ReAct 循环消息列表（System/Human/AI tool_calls/ToolMessage）
    documents: list[dict[str, Any]]  # 知识库文档清单
    intent: str  # 总图路由意图
    extracted_topics: list[dict[str, str]]  # 记忆提取结果
    confirmation: str  # 记忆写入确认回复
    rewritten_query: str  # 改写后的检索问题
    contexts: list[dict[str, Any]]  # 检索上下文分块
    sources: list[dict[str, Any]]  # 回答来源
    answer: str  # 最终回答文本
    run_id: str  # 当前流式运行 ID
    message_id: str  # 当前消息 ID
    tool_context: list[dict[str, Any]]  # 已执行工具结果
    tool_loop_count: int  # 工具循环轮次
