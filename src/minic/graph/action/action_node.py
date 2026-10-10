"""动作子图节点。"""  # 模块说明：记忆提取与动作回答

from __future__ import annotations  # 延迟注解求值

import json  # JSON 解析
import re  # 正则兜底
from typing import Any  # 任意类型

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # 消息类型

from minic.graph.common import (  # 公共工具
    append_documents_block,  # 追加文档清单块
    append_mcp_block,  # 追加 MCP 工具清单块
    memory_workspace,  # 记忆根目录
    merged_memory_text,  # 合并记忆
    response_messages,
    parse_model_json,  # JSON 解析
    stream_answer,  # 流式回答
    tool_system_prompt,  # 工具提示
)  # 导入结束
from minic.graph.state import ChatState  # 图状态
from minic.memory import LongTermMemoryStore, parse_topics  # 长期记忆存储
from minic.skills import skill_inject_text  # SKILL 注入文本


_FALLBACK_PATTERNS = [  # 个人信息兜底规则
    (re.compile(r"(?:我叫|我的名字是|我的名字叫)[：:\s]*([^\s，。；,!！?？]+)"), "姓名"),  # 姓名句式
    (re.compile(r"(?:我喜欢|我偏好|我偏爱|我爱用|我喜欢用)[：:\s]*([^\s，。；,!！?？]{1,40})"), "偏好"),  # 偏好句式
    (re.compile(r"(?:我住在|我的城市是|我来自)[：:\s]*([^\s，。；,!！?？]+)"), "位置"),  # 位置句式
    (re.compile(r"(?:我的职业是|我从事|我是一名|我是位)[：:\s]*([^\s，。；,!！?？]{1,30})"), "职业"),  # 职业句式
    (re.compile(r"(?:我是)([\u4e00-\u9fa5A-Za-z0-9]{1,20})"), "身份"),  # 身份句式
    (re.compile(r"(?:请记住|记住)[：:\s]*(.+)"), "备注"),  # 备注句式
]  # 规则结束

_GLOBAL_SCOPE_TOPICS = (  # 个人信息主题关键词
    "姓名",  # 姓名
    "名字",  # 名字
    "偏好",  # 偏好
    "兴趣",  # 兴趣
    "位置",  # 位置
    "城市",  # 城市
    "家乡",  # 家乡
    "职业",  # 职业
    "身份",  # 身份
    "习惯",  # 习惯
    "备注",  # 备注
    "年龄",  # 年龄
    "生日",  # 生日
    "联系方式",  # 联系方式
    "旅行",  # 旅行
)  # 关键词结束


def classify_scope(topic: str, content: str) -> str:  # 通用作用域分类
    """按信息类型通用分类：个人信息 -> global，其他 -> project。"""  # 函数说明
    del content  # 当前只用主题判断
    if any(keyword in topic for keyword in _GLOBAL_SCOPE_TOPICS):  # 命中个人信息关键词
        return "global"  # 归入全局记忆
    return "project"  # 其余归入项目记忆


def _fallback_extract(message: str) -> list[dict[str, str]]:  # 规则兜底提取
    """通用规则兜底：从第一人称陈述中提取个人信息。"""  # 函数说明
    if any(token in message for token in ("什么", "谁", "哪里", "吗", "？", "?")):  # 疑问句不提取
        return []  # 返回空结果
    extracted: list[dict[str, str]] = []  # 提取结果
    for pattern, topic in _FALLBACK_PATTERNS:  # 遍历兜底规则
        match = pattern.search(message)  # 匹配句式
        if not match:  # 未命中
            continue  # 继续下一条
        content = match.group(1).strip(" ,，。；!！?？")  # 清理提取内容
        if content:  # 内容非空
            extracted.append({"topic": topic, "content": content})  # 记录主题与内容
    return extracted  # 返回结果


async def extract_memory_node(  # 记忆提取节点
    state: ChatState,  # 图状态
    chat_model: Any,  # 聊天模型
    memory_store: LongTermMemoryStore | None,  # 长期记忆存储
) -> dict[str, Any]:  # 返回提取结果
    """从用户消息中提取信息并按 scope 写入长期记忆。"""  # 函数说明
    messages = [  # 组装提取消息
        SystemMessage(  # 系统提示
            content=(  # 提示内容
                "你是 MiniC 记忆提取器。从用户消息中提取稳定、可跨会话复用的信息。"  # 角色定义
                "只输出 JSON 数组，每项为 {\"topic\": \"主题\", \"content\": \"内容\", \"scope\": \"global|project\"}。"  # 输出格式
                "用户个人信息（姓名、偏好、位置、职业、身份、习惯、备注等）scope 为 global；"  # 全局分类规则
                "只有用户对当前项目明确陈述的长期约定、项目级偏好、可复用工作流 scope 为 project。"
                "临时任务参数、提问、引用资料、其他项目的信息不要保存为当前项目规则。"
                "不要解释，不要礼貌回应，不要评价内容。"  # 输出约束
                "示例：用户说“我的名字是张三” -> [{\"topic\": \"姓名\", \"content\": \"张三\", \"scope\": \"global\"}]；"  # 全局示例
                "用户说“这个项目约定用 Python” -> [{\"topic\": \"项目约定\", \"content\": \"用 Python\", \"scope\": \"project\"}]。"  # 项目示例
                "如果消息中没有可提取信息，输出 []。"  # 空结果说明
            )  # 内容结束
        ),  # 系统提示结束
        HumanMessage(content=state["user_message"]),  # 用户消息
    ]  # 消息结束
    response = await chat_model.ainvoke(messages)  # 调用模型并重试
    try:  # 解析提取结果
        topics = parse_model_json(response.content)  # 解析 JSON 数组
    except (json.JSONDecodeError, TypeError, AttributeError):  # 解析失败
        topics = []  # 按空结果处理
    extracted: list[dict[str, str]] = []  # 清洗后的提取结果
    for item in topics if isinstance(topics, list) else []:  # 遍历模型结果
        if not isinstance(item, dict):  # 跳过非对象项
            continue  # 继续
        topic = str(item.get("topic", "")).strip()  # 读取主题
        content = str(item.get("content", "")).strip()  # 读取内容
        scope = str(item.get("scope", "")).strip()  # 读取作用域
        if (  # 校验字段
            topic  # 主题非空
            and content  # 内容非空
            and len(content) <= 80  # 内容长度受限
            and not any(keyword in content.lower() for keyword in ("meaning", "acronym", "abbreviation", "取决于", "无法确定"))  # 排除无效内容
        ):  # 校验结束
            if scope not in {"global", "project"}:  # 模型未给有效 scope
                scope = classify_scope(topic, content)  # 模型未给 scope 时按信息类型兜底
            extracted.append({"topic": topic, "content": content, "scope": scope})  # 保留主题、内容与作用域
    if not extracted:  # 模型未提取到
        extracted = [  # 使用规则兜底
            {**item, "scope": classify_scope(item["topic"], item["content"])}  # 为兜底结果补作用域
            for item in _fallback_extract(state["user_message"])  # 遍历兜底结果
        ]  # 兜底结束
    confirmations: list[str] = []
    for item in extracted:  # 写入长期记忆
        if memory_store is not None:  # 有记忆存储
            scope = item["scope"]
            workspace = memory_workspace(state) if scope == "project" else None
            changed = memory_store.add_topic(scope, workspace, item["topic"], item["content"], source="user")
            saved = memory_store.read(scope, workspace)
            label = "项目记忆" if scope == "project" else "全局记忆"
            if parse_topics(saved["content"]).get(item["topic"]) == item["content"]:
                status = "已保存" if changed else "已存在，无需重复保存"
                confirmations.append(f"{status}：{label}「{item['topic']}」\n文件：{saved['path']}")
            else:
                confirmations.append(f"未保存：{label}「{item['topic']}」，已有记忆保护规则阻止了写入。")
        else:
            confirmations.append(f"未保存：「{item['topic']}」，长期记忆存储未启用。")
    result: dict[str, Any] = {  # 节点返回结果
        "extracted_topics": extracted,  # 提取结果
        "confirmation": "\n\n".join(confirmations),  # 基于实际落盘结果确认
    }  # 结果结束
    if extracted:  # 有提取结果
        result["intent"] = "user_info"  # 有提取结果时动作子图走确认回复
    return result  # 返回结果


async def agent_node(  # ReAct 决策节点
    state: ChatState,  # 图状态
    chat_model: Any,  # 聊天模型
    memory_store: LongTermMemoryStore | None,  # 长期记忆存储
    settings: Any,  # 应用配置
    skill_manager: Any = None,  # SKILL 管理器
) -> dict[str, Any]:  # 返回消息增量
    """教科书式 ReAct 的 agent 节点：绑定工具原生调用，输出 AIMessage。

    首轮构建初始消息（系统提示 + 历史 + 用户消息）；后续轮次直接
    续传 ``state["messages"]``（含上一轮 AIMessage(tool_calls) 与
    ToolMessage，langchain 原样回传 reasoning_content，DeepSeek thinking
    模式安全）。
    """
    from langchain_core.messages import message_chunk_to_message
    from minic.graph.native_tools import native_tools
    from minic.graph.common import model_messages
    messages = model_messages(state, settings)
    if not messages:
        messages = [HumanMessage(content=state["user_message"])]
    system_text = append_mcp_block(append_documents_block(tool_system_prompt(), state.get("documents") or []))
    memory_text = merged_memory_text(state, memory_store, settings)
    if memory_text:
        system_text += f"\n\n项目约定与长期记忆：\n{memory_text}"
    skill_text = skill_inject_text(skill_manager)
    if skill_text:
        system_text += f"\n\n当前启用的 SKILL 能力：\n{skill_text}"
    tools = native_tools() if int(state.get("tool_loop_count", 0)) < 8 else []
    if not tools:
        system_text += "\n本轮工具预算已用尽，请依据已有结果总结完成事项、未完成事项和下一步，不再调用工具。"
    bound = chat_model.bind_tools(tools) if tools else chat_model
    messages = [SystemMessage(content=system_text), *messages]
    chunks: list[Any] = []  # 流式 chunk
    async for chunk in bound.astream(messages):  # 流式调用（最终答案 token 实时转发）
        chunks.append(chunk)  # 收集
    if chunks:  # 合并分片为完整消息
        response: Any = chunks[0]  # 首个分片
        for extra in chunks[1:]:  # 逐片合并
            response = response + extra  # AIMessageChunk 相加合并
    else:  # 无输出
        response = AIMessage(content="")  # 空消息
    response = message_chunk_to_message(response)
    if response.tool_calls and not tools:
        closed = [ToolMessage(content="本轮工具预算已用尽，该调用未执行", tool_call_id=call["id"]) for call in response.tool_calls]
        return {"messages": response_messages(state, [response, *closed, AIMessage(content="本轮工具预算已用尽，已完成的操作保留在执行记录中。请检查结果后继续未完成的事项。")])}
    return {"messages": response_messages(state, [response])}


async def tools_node(  # ReAct 工具执行节点
    state: ChatState,  # 图状态
    tool_runtime: Any = None,  # 共享工具执行核心
) -> dict[str, Any]:  # 返回 ToolMessage 增量
    """执行 agent 最后一条 AIMessage 的 tool_calls，生成 ToolMessage。

    每个调用走 tool_runtime（含审批中断、沙箱、事件发射），结果记录
    进 tool_context；ToolMessage 原样入 messages（短期记忆同步落盘）。
    """
    from minic.graph.native_tools import execute_calls
    return await execute_calls(state, tool_runtime)



def should_continue(state: ChatState) -> str:  # ReAct 条件边
    """agent 输出含 tool_calls 且未超轮数上限 → 继续工具；否则进回答。"""
    messages = state.get("messages") or []  # 消息列表
    last = messages[-1] if messages else None  # 最后一条
    if isinstance(last, AIMessage) and last.tool_calls:  # 有工具调用
        if int(state.get("tool_loop_count", 0)) < 8:  # 未超上限
            return "tools"  # 继续执行工具
    return "answer"  # 进入回答


_TOOL_NULL_MARKER = re.compile(r'^\s*\{\s*"tool"\s*:\s*null\s*\}\s*$')  # 旧纯文本协议空调用标记


def _extract_final_answer(state: ChatState) -> str:  # 提取最终回答
    """从 ReAct 消息列表提取 agent 最终回答文本（最后一条无 tool_calls 的 AIMessage）。

    模型偶发输出旧协议标记 ``{"tool": null}`` 时视为空回答并跳过，
    由回答节点兜底重新生成（不在界面上显示裸标记）。
    """
    messages = state.get("messages") or []  # 消息列表
    last = messages[-1] if messages else None
    if isinstance(last, AIMessage) and not last.tool_calls:
        content = str(last.content or "").strip()
        if content and not _TOOL_NULL_MARKER.match(content):
            return content
    return ""  # 未产出文本


async def action_answer_node(  # 动作回答节点
    state: ChatState,  # 图状态
    chat_model: Any,  # 聊天模型
    memory_store: LongTermMemoryStore | None,  # 长期记忆存储
    settings: Any,  # 应用配置
    skill_manager: Any = None,  # SKILL 管理器
) -> dict[str, Any]:  # 返回回答
    """按意图处理：chat_action 提取 agent 最终回答；user_info/memory_query 单独生成。"""  # 函数说明
    from minic.graph.common import model_messages
    intent = state.get("intent", "chat_action")  # 读取意图
    memory_text = merged_memory_text(state, memory_store, settings)  # 读取合并记忆
    if intent == "user_info":  # 记忆操作由系统执行，回执直接采用实际结果
        answer = state.get("confirmation") or "本轮没有可确认的记忆保存结果。"
        return {"answer": answer, "messages": response_messages(state, [AIMessage(content=answer)])}
    elif intent == "memory_query":  # 用户询问自己的信息
        if memory_text:  # 有记忆
            system_text = (  # 记忆回答提示
                "你是 MiniC 助手。请根据长期记忆回答用户关于自己的问题或当前项目背景、约定和长期规则的问题。"
                "直接输出纯文本答案，不要使用 JSON 包装。\n\n"  # 输出约束
                f"长期记忆：\n{memory_text}"  # 注入记忆
            )  # 提示结束
        else:  # 无记忆
            system_text = "你是 MiniC 助手。没有找到相关长期记忆，请说明未检索到足够依据，不要编造。"
    else:  # 普通聊天：教科书式提取 agent 最终回答（agent 已基于工具结果作答）
        answer = _extract_final_answer(state)  # 提取最终回答
        if answer:  # agent 已作答
            return {"answer": answer}  # 直接返回（不再二次调用模型）
        # 兜底：agent 未产出文本（异常/无工具决策路径）时重新生成
        system_text = (  # 普通回答提示
            "你是 MiniC 助手。请直接回答用户，不要使用 JSON 包装。"  # 输出约束
            "你具备工具能力，可以直接访问文件系统与执行命令；"  # 能力声明
            "不要声称自己无法访问文件系统或计算机。"  # 禁止推脱
            "禁止输出任何工具调用标记（DSML、XML、JSON 工具调用等），"  # 禁止标记
            "如需继续操作请直接用自然语言说明。"  # 自然语言说明
        )  # 提示结束
        system_text = append_documents_block(system_text, state.get("documents") or [])  # 常驻文档清单
        system_text = append_mcp_block(system_text)  # 常驻已连接 MCP 工具清单
        if memory_text:
            system_text += f"\n\n长期记忆：\n{memory_text}"
        tool_context = state.get("tool_context", [])  # 工具结果
        if tool_context:  # 有工具结果
            tool_block = "\n\n".join(  # 拼接结果块
                f"[{index + 1}] {item.get('tool')} 状态：{item.get('status')}\n{item.get('output') or '（无输出）'}"  # 单条结果
                for index, item in enumerate(tool_context)  # 编号结果
            )  # 结果块结束
            system_text += (  # 追加结果提示
                f"\n\n工具执行结果：\n{tool_block}\n"  # 结果文本
                "请基于工具执行结果回答，不要编造。"  # 回答约束
            )  # 追加结束
        skill_text = skill_inject_text(skill_manager)  # 读取启用的 SKILL 注入文本
        if skill_text:  # 有启用的 SKILL
            system_text += f"\n\n当前启用的 SKILL 能力：\n{skill_text}"  # 注入 SKILL 描述
        answer = await stream_answer(  # 流式生成回答
            chat_model,  # 模型
            system_text,  # 系统提示
            state.get("history", []),  # 历史
            state["user_message"],  # 用户消息
            native_messages=model_messages(state, settings),
        )  # 回答结束
        return {"answer": answer, "messages": response_messages(state, [AIMessage(content=answer)])}  # 返回回答
    skill_text = skill_inject_text(skill_manager)  # 读取启用的 SKILL 注入文本
    if skill_text:  # 有启用的 SKILL
        system_text += f"\n\n当前启用的 SKILL 能力：\n{skill_text}"  # 注入 SKILL 描述
    answer = await stream_answer(  # 流式生成回答（user_info/memory_query 分支）
        chat_model,  # 模型
        system_text,  # 系统提示
        state.get("history", []),  # 历史
        state["user_message"],  # 用户消息
            native_messages=model_messages(state, settings),
    )  # 回答结束
    return {"answer": answer, "messages": response_messages(state, [AIMessage(content=answer)])}  # 返回回答
