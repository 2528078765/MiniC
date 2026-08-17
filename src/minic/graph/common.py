"""图公共工具函数。"""  # 模块说明：图包通用工具

from __future__ import annotations  # 延迟注解求值

import json  # JSON 解析
import re  # 正则提取
from typing import Any  # 任意类型

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # 消息类型

from minic.graph.state import ChatState  # 图状态
from minic.graph.tools import list_tools  # 工具清单
from minic.middleware import memory_injection  # 记忆注入


def unwrap_json_answer(answer: str) -> str:  # 解包模型 JSON 答案
    """去掉模型偶发输出的 JSON 包装，返回纯文本答案。"""  # 函数说明
    text = answer.strip()  # 去掉首尾空白
    if text.startswith("```"):  # 可能是代码块包装
        lines = text.splitlines()  # 按行拆分
        if lines and lines[0].startswith("```"):  # 去掉开头围栏
            lines = lines[1:]  # 移除首行
        if lines and lines[-1].strip() == "```":  # 去掉结尾围栏
            lines = lines[:-1]  # 移除末行
        text = "\n".join(lines).strip()  # 重组文本
    try:  # 尝试解析 JSON
        data = json.loads(text)  # 解析为对象
    except (json.JSONDecodeError, TypeError):  # 不是 JSON 时原样返回
        return text  # 返回纯文本
    if isinstance(data, dict):  # 只处理字典包装
        for key in ("answer", "content", "text", "response"):  # 常见答案字段
            value = data.get(key)  # 读取字段值
            if isinstance(value, str) and value.strip():  # 非空字符串才采用
                return value.strip()  # 返回字段内容
    return text  # 无匹配字段时返回原文本


def parse_model_json(text: str) -> Any:  # 解析模型 JSON 输出
    """解析模型返回的 JSON，兼容代码块包装。"""  # 函数说明
    text = text.strip()  # 去掉首尾空白
    if text.startswith("```"):  # 可能是代码块包装
        lines = text.splitlines()  # 按行拆分
        if lines and lines[0].startswith("```"):  # 去掉开头围栏
            lines = lines[1:]  # 移除首行
        if lines and lines[-1].strip() == "```":  # 去掉结尾围栏
            lines = lines[:-1]  # 移除末行
        text = "\n".join(lines).strip()  # 重组文本
    try:  # 直接解析
        return json.loads(text)  # 成功则返回对象
    except json.JSONDecodeError:  # 整体解析失败时尝试提取 JSON 片段
        match = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)  # 查找数组或对象
        if match:  # 找到片段
            return json.loads(match.group(1))  # 解析片段
        raise  # 无法解析则抛出


# DSML 工具调用格式（DeepSeek 系模型原生输出），标签变体繁多：
# 开标签：<|DSML| tool_calls> / <| DSML | invoke name="Bash">
# 结束标签：
_DSML_BLOCK = re.compile(r"<[^>]*(?:DSML|⊙)[^>]*>[\s\S]*?</[^>]*(?:DSML|⊙)[^>]*>")  # 成对 DSML 块（含参数值）
_DSML_TAG = re.compile(r"</?[^>]*(?:DSML|⊙)[^>]*>")  # 孤立 DSML 标签（含结束标签）
_DSML_OPEN = re.compile(r"<(?!/)[^>]*(?:DSML|⊙)[^>]*>")  # DSML 开标签
_DSML_CLOSE = re.compile(r"</[^>]*(?:DSML|⊙)[^>]*>")  # DSML 闭合标签

# 知识库概况类问题模式（如"你都知道什么"）：直接回答文档清单，不做向量检索
_OVERVIEW_PATTERNS = (
    "你都知道什么",
    "你知道什么",
    "知道些什么",
    "知识库里有什么",
    "知识库有哪些",
    "有哪些文档",
    "都有哪些文档",
    "都有哪些知识",
)


def is_knowledge_overview(text: str) -> bool:  # 判断知识库概况类问题
    """判断用户消息是否为询问知识库概况的问题（如"你都知道什么"）。"""  # 函数说明
    return any(pattern in text for pattern in _OVERVIEW_PATTERNS)  # 任一模式命中即成立


def render_document_lines(documents: list[dict[str, Any]]) -> str:  # 渲染文档清单
    """把知识库文档清单渲染成提示文本（空清单返回空串）。"""  # 函数说明
    return "\n".join(  # 每行一个文档
        f"- {item.get('file_path') or '未知文件'}（{item.get('chunk_count', 0)} 分块）"  # 文档行
        for item in documents[:50]  # 最多取前 50 个文档
    )  # 拼接结束


def append_documents_block(system_text: str, documents: list[dict[str, Any]]) -> str:  # 追加清单块
    """在系统提示词末尾追加知识库文档清单块（有清单才追加）。

    清单常驻系统提示词后，模型无论走哪个回答分支都能看到，
    用户用任何说法询问知识库概况（"你都知道什么"的各种改述）都能按清单回答。
    """  # 函数说明
    lines = render_document_lines(documents)  # 渲染清单文本
    if not lines:  # 空库不追加
        return system_text  # 原样返回
    return system_text + (  # 追加清单块
        f"\n\n知识库文档清单：\n{lines}"  # 清单文本
        "\n（若用户询问知识库概况，例如“你都知道什么”的各种说法，请直接按清单回答文档名。）"  # 概况例外
    )  # 返回结束


def mcp_tools_text(max_count: int = 20, max_desc: int = 80) -> str:  # 渲染 MCP 工具清单
    """返回已连接 MCP 工具清单文本（名称 + 截断描述）；无 MCP 工具返回空串。"""  # 函数说明
    from minic.graph.tools import list_tools  # 延迟导入避免循环依赖

    lines: list[str] = []  # 工具行
    for tool in list_tools():  # 遍历全部注册工具
        if getattr(tool, "category", "") != "mcp":  # 只取 MCP 工具
            continue  # 跳过内置工具
        description = (tool.description or "").strip().replace("\n", " ")  # 单行化描述
        lines.append(f"- {tool.name}：{description[:max_desc]}")  # 工具行
    if not lines:  # 无已连接 MCP 工具
        return ""  # 不注入
    return "\n".join(lines[:max_count])  # 限长返回


def append_mcp_block(system_text: str) -> str:  # 追加 MCP 清单块
    """在系统提示词末尾追加已连接 MCP 工具清单块（无 MCP 工具不追加）。

    模型回答"你连接了哪些 MCP/工具能力"时有真实数据可用，
    不再依赖检索资料误答；需要时也可直接调用这些工具。
    """  # 函数说明
    text = mcp_tools_text()  # 渲染 MCP 工具清单
    if not text:  # 无 MCP 工具
        return system_text  # 原样返回
    return system_text + (  # 追加清单块
        f"\n\n当前已连接的 MCP 工具：\n{text}"  # 清单文本
        "\n（用户询问你连接了哪些 MCP 或工具能力时，请依据此清单如实回答；"
        "完成任务需要时可直接调用这些工具。）"  # 使用指示
    )  # 返回结束



def strip_dsml(text: str) -> str:  # 剔除 DSML 标记文本
    """把模型输出中的 DSML 工具调用标记（含参数值）全部剔除，返回纯文本。"""  # 函数说明
    previous = None  # 上一轮结果
    while previous != text:  # 反复剔除直到稳定（嵌套/连续块）
        previous = text  # 记录本轮输入
        text = _DSML_BLOCK.sub("", text)  # 删成对块
        text = _DSML_TAG.sub("", text)  # 删孤立标签
    text = re.sub(r"\n{3,}", "\n\n", text)  # 归并删除后残留的多余空行
    return text.strip()  # 返回干净文本


def split_incomplete_dsml(text: str) -> tuple[str, str]:  # 流式 DSML 过滤
    """流式输出过滤：删除完整 DSML 块/标签，返回 (未闭合残留, 干净文本)。

    跨 chunk 的 DSML 块在闭合前不会显示：未闭合的开标签连同其后内容
    作为残留缓冲，闭合标签到达（成对块）后才放行干净文本。
    """
    if "DSML" not in text and "\u2299" not in text:  # 无 DSML 迹象
        index = text.rfind("<")  # 未闭合尾部可能是标签前奏
        if index != -1 and ">" not in text[index:]:
            return text[index:], text[:index]  # 缓冲尾部 "<"，防止开标签前奏放行后丢失
        return "", text  # 干净放行
    cleaned = _DSML_BLOCK.sub("", text)  # 删成对块
    cleaned = _DSML_CLOSE.sub("", cleaned)  # 删孤立闭合标签
    if "DSML" in cleaned or "\u2299" in cleaned:  # 仍有未闭合 DSML 块
        index = cleaned.find("<")  # 从第一个 "<" 起整体缓冲（不能 rfind，会漏掉前面开标签）
        if index != -1:
            return cleaned[index:], cleaned[:index]  # 缓冲未闭合块
        return "", cleaned  # 只有 DSML 字样无标签符号：按普通文本放行
    return "", cleaned  # 无残留


def parse_dsml_tool_call(text: str) -> dict[str, Any]:  # 解析 DSML 工具调用
    """从模型输出的 DSML 文本解析工具调用；无 invoke 时抛 ValueError。"""  # 函数说明
    invoke = re.search(r"<\s*\|?\s*DSML\s*\|?\s*invoke\s+name=\"([^\"]+)\"", text)  # 找 invoke 名
    if not invoke:  # 无调用声明
        raise ValueError("DSML 中无 invoke")  # 视为不可解析
    tool = invoke.group(1).strip()  # 工具名
    args: dict[str, Any] = {}  # 参数容器
    for match in re.finditer(  # 逐个参数
        r"<\s*\|?\s*DSML\s*\|?\s*parameter\s+name=\"([^\"]+)\"[^>]*>([\s\S]*?)<\s*/\s*\|?\s*DSML\s*\|?\s*parameter>",  # 参数块
        text,  # 匹配源
    ):  # 遍历参数
        name, raw_value = match.group(1).strip(), match.group(2).strip()  # 名与原始值
        args[name] = raw_value  # 先按字符串保存
    if not args:  # 无参数时仍可调用（空参数工具）
        return {"tool": tool, "args": {}}  # 空参数返回
    return {"tool": tool, "args": args}  # 返回工具与参数


def memory_workspace(state: ChatState) -> str:  # 计算项目记忆落盘根目录
    """返回项目长期记忆落盘根目录，优先使用 project_root。"""  # 函数说明
    return str(state.get("project_root") or state.get("workspace", ""))  # 有 project_root 则优先


def merged_memory_text(  # 读取合并记忆文本
    state: ChatState,  # 图状态
    memory_store: Any,  # 长期记忆存储
    settings: Any,  # 应用配置
) -> str:  # 返回注入后的记忆文本
    """读取并注入合并后的长期记忆。"""  # 函数说明
    if memory_store is None:  # 无记忆存储时不注入
        return ""  # 返回空文本
    memory_data = memory_store.read("merged", memory_workspace(state))  # 合并读取全局与项目长期记忆
    threshold = getattr(getattr(settings, "memory", None), "long_term_inject_threshold_tokens", 4000)  # 读取注入阈值
    return memory_injection(memory_data.get("content", ""), threshold)  # 超过阈值时注入摘要


def tool_system_prompt() -> str:  # 生成工具调用系统提示
    """返回可调用工具清单提示文本。"""  # 函数说明
    lines = []  # 工具行
    for tool in list_tools():  # 遍历注册表
        args_text = ", ".join(tool.args_schema.keys())  # 参数名
        lines.append(f"- {tool.name}: {tool.description}（参数：{args_text}）")  # 工具描述
    return (  # 组合提示
        "你是 MiniC 工具调用器。根据用户消息、会话历史和已执行工具结果决定是否调用工具。\n"  # 角色
        f"可用工具：\n{chr(10).join(lines)}\n"  # 工具清单
        "执行环境是 Windows，Bash 命令在 cmd.exe 中运行："  # 环境说明
        "请使用 Windows 命令与路径（dir、type、echo、C:\\Users\\<用户名>），"  # Windows 命令
        "用户主目录可用命令 `echo %USERPROFILE%` 获取，"  # 主目录获取方式
        "不要使用 ls、~、/home、/dev 等 Linux 语法。\n"  # 禁止 Linux 语法
        "需要调用工具时使用函数调用（function calling）；"  # 原生工具调用
        "不需要调用工具时直接用自然语言回答用户，"  # 直接回答
        "不要输出 JSON、DSML、XML 等任何工具调用标记。"  # 输出约束
    )  # 提示结束


def build_messages(  # 组装模型消息
    system_text: str,  # 系统提示
    history: list[dict[str, Any]],  # 会话历史
    user_message: str,  # 用户消息
    tool_context: list[dict[str, Any]] | None = None,  # 工具结果
) -> list[Any]:  # 返回消息列表
    """组装系统、历史、工具结果与用户消息。"""  # 函数说明
    messages = [SystemMessage(content=system_text)]  # 系统消息
    for message in history[:-1]:  # 历史最后一条是本次消息，跳过
        content = message.get("content", "")  # 消息内容
        if message.get("role") == "ai":  # AI 消息
            messages.append(AIMessage(content=content))  # 追加 AI 消息
        else:  # 人类消息
            messages.append(HumanMessage(content=content))  # 追加人类消息
    if tool_context:  # 有工具结果
        for index, entry in enumerate(tool_context):  # 遍历结果
            tool_call_id = str(entry.get("tool_call_id") or f"tool-{index}")  # 缺失时用稳定占位
            args = entry.get("args") if isinstance(entry.get("args"), dict) else {}  # 参数兜底
            messages.append(  # 前置配对 AI 消息
                AIMessage(  # 声明本次工具调用
                    content="",  # 无文本内容
                    tool_calls=[  # 工具调用声明
                        {  # 单个调用
                            "name": str(entry.get("tool") or "unknown"),  # 工具名
                            "args": args,  # 调用参数
                            "id": tool_call_id,  # 调用 ID 与 ToolMessage 一致
                        }  # 调用结束
                    ],  # 声明结束
                )  # AI 消息结束
            )  # 追加结束
            content = entry.get("output") or entry.get("status") or ""  # 结果文本
            messages.append(  # 追加工具消息
                ToolMessage(  # LangChain 工具消息
                    content=str(content),  # 结果内容
                    tool_call_id=tool_call_id,  # 与前置 AI 消息 ID 配对
                )  # 工具消息结束
            )  # 追加结束
    messages.append(HumanMessage(content=user_message))  # 本次用户消息
    return messages  # 返回消息


async def stream_answer(  # 流式生成回答
    chat_model: Any,  # 聊天模型
    system_text: str,  # 系统提示词
    history: list[dict[str, Any]],  # 会话历史
    user_message: str,  # 用户消息
    tool_context: list[dict[str, Any]] | None = None,  # 工具结果
) -> str:  # 返回回答文本
    """按系统提示生成流式回答并解包 JSON。"""  # 函数说明
    messages = build_messages(system_text, history, user_message, tool_context)  # 组装消息
    parts: list[str] = []  # 收集回答分片
    async for chunk in chat_model.astream(messages):  # 流式调用模型
        if chunk.content:  # 忽略空分片
            parts.append(chunk.content)  # 累积分片
    return unwrap_json_answer(strip_dsml("".join(parts)))  # 剔除偶发 DSML 工具标记后解包返回纯文本
