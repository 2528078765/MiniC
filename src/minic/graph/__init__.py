"""MiniC 图模块公共 API。"""  # 模块说明：对外导出图包公共符号

from minic.graph.action.action_node import classify_scope  # 作用域分类
from minic.graph.common import unwrap_json_answer  # JSON 答案解包
from minic.graph.state import ChatState  # 图状态类型
from minic.graph.super_graph import build_chat_graph, build_super_graph  # 总图构建入口
from minic.graph.tools import (  # 工具注册表
    ToolSpec,  # 工具定义
    clear_mcp_tools,  # 清空 MCP 工具
    get_tool,  # 按名称取工具
    list_tools,  # 列出全部工具
    register_mcp_tools,  # 注册 MCP 工具
)

__all__ = [  # 公开导出列表
    "ChatState",  # 图状态类型
    "ToolSpec",  # 工具定义
    "build_chat_graph",  # 兼容入口
    "build_super_graph",  # 总图构建
    "classify_scope",  # 作用域分类
    "clear_mcp_tools",  # 清空 MCP 工具
    "get_tool",  # 按名称取工具
    "list_tools",  # 列出全部工具
    "register_mcp_tools",  # 注册 MCP 工具
    "unwrap_json_answer",  # JSON 解包
]
