"""MiniC 桌面端复用控件：toast / 开关 / 工具卡片 / 消息气泡 / 弹窗。"""

from __future__ import annotations

from minic.gui.widgets.dialogs import ApprovalDialog, ConfigDialog, InputDialog
from minic.gui.widgets.message_bubble import MessageBubble
from minic.gui.widgets.toast import Toast
from minic.gui.widgets.toggle import ToggleSwitch
from minic.gui.widgets.tool_card import ToolCard

__all__ = [
    "ApprovalDialog",
    "ConfigDialog",
    "InputDialog",
    "MessageBubble",
    "Toast",
    "ToggleSwitch",
    "ToolCard",
]
