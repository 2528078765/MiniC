"""MCP 配置加载（规格书 14.1）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_MCP_SETTINGS_PATH = Path.home() / ".minic" / "mcp" / "minic_mcp_settings.json"  # 用户级默认配置路径


def load_mcp_settings(path: str | Path | None = None) -> dict[str, Any]:
    """读取 MCP 服务配置。

    文件不存在时返回空的 ``{"mcpServers": {}}``；
    JSON 解析失败时抛出 ``ValueError``。

    Args:
        path: 配置文件路径；默认使用 ``~/.minic/mcp/minic_mcp_settings.json``。

    Returns:
        配置字典，结构为 ``{"mcpServers": {name: {...}}}``。

    Raises:
        ValueError: JSON 解析失败时抛出。
    """
    settings_path = Path(path) if path is not None else DEFAULT_MCP_SETTINGS_PATH
    if not settings_path.exists():
        return {"mcpServers": {}}
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"MCP 配置文件不是合法 JSON: {settings_path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"MCP 配置文件必须是一个对象: {settings_path}")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        raise ValueError(f"MCP 配置缺少 mcpServers 对象: {settings_path}")
    return {"mcpServers": servers}
