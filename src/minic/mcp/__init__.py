"""MiniC MCP 客户端能力包。

G7a 接入 Model Context Protocol：配置加载、连接管理、工具注册与调用。
"""

from minic.mcp.client import McpManager  # 客户端管理器
from minic.mcp.settings import load_mcp_settings  # 配置加载

__all__ = ["McpManager", "load_mcp_settings"]  # 公共 API
