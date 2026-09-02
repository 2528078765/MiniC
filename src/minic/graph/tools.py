"""工具定义注册表。"""  # 模块说明：工具元数据

from __future__ import annotations  # 延迟注解求值

from dataclasses import dataclass  # 数据类
from typing import Any  # 任意类型


@dataclass(frozen=True)  # 不可变数据类
class ToolSpec:  # 工具定义
    """工具元数据定义。"""  # 类说明

    name: str  # 工具名称
    description: str  # 工具描述
    args_schema: dict[str, Any]  # 参数 schema
    category: str  # "read" | "write"


_TOOL_REGISTRY: dict[str, ToolSpec] = {  # 工具注册表
    "Read": ToolSpec(  # 读取工具
        name="Read",  # 名称
        description="读取工作区内文件",  # 描述
        args_schema={"path": str, "start_line": int, "end_line": int},  # 参数
        category="read",  # 只读分类
    ),  # 结束
    "ReadMemory": ToolSpec(  # 读记忆工具
        name="ReadMemory",  # 名称
        description="按主题读取长期记忆",  # 描述
        args_schema={"topic": str},  # 参数
        category="read",  # 只读分类
    ),  # 结束
    "TextSearch": ToolSpec(  # 文本搜索工具
        name="TextSearch",  # 名称
        description="在工作区文本中搜索正则表达式",  # 描述
        args_schema={"path": str, "pattern": str},  # 参数
        category="read",  # 只读分类
    ),  # 结束
    "Lint": ToolSpec(  # 语法检查工具
        name="Lint",  # 名称
        description="对 Python 文件做语法检查",  # 描述
        args_schema={"path": str},  # 参数
        category="read",  # 只读分类
    ),  # 结束
    "Write": ToolSpec(  # 写入工具
        name="Write",  # 名称
        description="写入文件",  # 描述
        args_schema={"path": str, "content": str},  # 参数
        category="write",  # 写分类
    ),  # 结束
    "Edit": ToolSpec(  # 编辑工具
        name="Edit",  # 名称
        description="增量替换文件内容",  # 描述
        args_schema={"path": str, "old_text": str, "new_text": str},  # 参数
        category="write",  # 写分类
    ),  # 结束
    "Format": ToolSpec(  # 格式化工具
        name="Format",  # 名称
        description="简单格式化文件",  # 描述
        args_schema={"path": str},  # 参数
        category="write",  # 写分类
    ),  # 结束
    "Bash": ToolSpec(  # 终端命令工具
        name="Bash",  # 名称
        description="执行终端命令，需要人工审批",  # 描述
        args_schema={"command": str},  # 参数
        category="exec",  # 执行分类（full 权限）
    ),  # 结束
    "GitStatus": ToolSpec(  # Git 状态工具
        name="GitStatus",  # 名称
        description="查看工作区 Git 状态",  # 描述
        args_schema={},  # 参数
        category="read",  # 只读分类
    ),  # 结束
    "GitDiff": ToolSpec(  # Git 差异工具
        name="GitDiff",  # 名称
        description="查看工作区改动差异",  # 描述
        args_schema={"path": str},  # 参数
        category="read",  # 只读分类
    ),  # 结束
    "GitLog": ToolSpec(  # Git 日志工具
        name="GitLog",  # 名称
        description="查看最近提交记录",  # 描述
        args_schema={"n": int},  # 参数
        category="read",  # 只读分类
    ),  # 结束
    "GitCommit": ToolSpec(  # Git 提交工具
        name="GitCommit",  # 名称
        description="暂存全部改动并提交",  # 描述
        args_schema={"message": str},  # 参数
        category="write",  # 写分类
    ),  # 结束
    "GitBranch": ToolSpec(  # Git 分支工具
        name="GitBranch",  # 名称
        description="新建 Git 分支",  # 描述
        args_schema={"name": str},  # 参数
        category="write",  # 写分类
    ),  # 结束
    "DelegateToSubagent": ToolSpec(  # 子 Agent 委托工具
        name="DelegateToSubagent",  # 名称
        description="把子任务委托给子 Agent 执行，返回子 Agent 最终输出",  # 描述
        args_schema={"task": str, "allowed_tools": list, "budget": int},  # 参数
        category="exec",  # 执行分类（委托/全权）
    ),  # 结束
    "IngestDirectory": ToolSpec(  # 知识库入库工具
        name="IngestDirectory",  # 名称
        description=(
            "把 Markdown/TXT 目录增量入库到 RAG 知识库，调用会触发人工审批，可能产生 embedding 费用。"
            "总结入库工作流：先用 Write 写 Markdown 总结到知识库目录，再调用本工具入库。"
        ),  # 描述（含工作流引导）
        args_schema={"path": str},  # 参数（可选，缺省用 rag.default_directory）
        category="exec",  # 执行分类（入库/embedding 成本）
    ),  # 结束
}  # 注册表结束

_MCP_TOOLS: dict[str, ToolSpec] = {}  # MCP 工具注册表（server_name.tool_name）


def _json_schema_to_args(schema: dict[str, Any]) -> dict[str, Any]:
    """把 MCP 工具的 JSON Schema 转成 ToolSpec 的参数名列表结构。"""
    properties = schema.get("properties") or {}
    args: dict[str, Any] = {}
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            args[name] = "any"
            continue
        json_type = spec.get("type", "any")
        type_map = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
            "object": dict,
            "null": "any",
        }
        args[name] = type_map.get(json_type, "any")
    return args


def register_mcp_tools(mcp_manager: Any) -> int:
    """把已连接 MCP server 的工具注册到注册表，返回注册数量。

    工具注册名为 ``server_name.tool_name``，category 固定为 ``mcp``。
    每次调用会按当前管理器连接状态重建 MCP 工具集合（幂等）。
    """
    registered: dict[str, ToolSpec] = {}
    for server_name, schema in mcp_manager.tools():
        tool_name = schema["name"]
        registered[f"{server_name}.{tool_name}"] = ToolSpec(
            name=f"{server_name}.{tool_name}",
            description=schema.get("description", ""),
            args_schema=_json_schema_to_args(schema.get("args_schema") or {}),
            category="mcp",
        )
    _MCP_TOOLS.clear()
    _MCP_TOOLS.update(registered)
    return len(registered)


def clear_mcp_tools() -> None:
    """清空 MCP 工具注册（应用关闭或测试隔离时调用）。"""
    _MCP_TOOLS.clear()


def get_tool(name: str) -> ToolSpec | None:  # 按名称取工具
    """按名称返回工具定义。"""  # 函数说明
    return _TOOL_REGISTRY.get(name) or _MCP_TOOLS.get(name)  # 内置与 MCP 工具合并查找


def list_tools() -> list[ToolSpec]:  # 列出全部工具
    """返回全部工具定义（内置 + MCP）。"""  # 函数说明
    return list(_TOOL_REGISTRY.values()) + list(_MCP_TOOLS.values())  # 返回注册表全部值
