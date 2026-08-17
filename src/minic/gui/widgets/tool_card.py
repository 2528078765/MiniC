"""ToolCard：工具调用步骤（紧凑单行：图标 + 动作描述 + 状态，可点开看参数/输出）。"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QTransform
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from minic.gui.icons import load_pixmap
from minic.gui.theme import (
    COLOR_ACCENT,
    COLOR_BG_ACTIVE,
    COLOR_BORDER,
    COLOR_GREEN,
    COLOR_RED,
    COLOR_SHIELD,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
)
from minic.gui.widgets.spinner import SpinnerLabel

_STATUS_STYLE = {
    "success": (COLOR_GREEN, "完成"),
    "denied": (COLOR_RED, "已拒绝"),
    "failed": (COLOR_RED, "失败"),
    "running": (COLOR_ACCENT, "执行中"),
    "awaiting": (COLOR_SHIELD, "需审批"),
    "approved": (COLOR_GREEN, "已批准"),
    "expired": (COLOR_SHIELD, "审批超时"),
    "cancelled": (COLOR_RED, "已取消"),
}

# 工具名 → (图标, 动作词)；“动作词 + 目标”组成一行描述
_TOOL_META: dict[str, tuple[str, str]] = {
    "Read": ("📄", "读取"),
    "Write": ("✏️", "写入"),
    "Edit": ("✏️", "编辑"),
    "Bash": ("⌘", "执行"),
    "TextSearch": ("🔍", "搜索"),
    "RAGQuery": ("🔍", "RAG 查询"),
    "RAGSearch": ("🔍", "RAG 查询"),
    "IngestDirectory": ("🗂", "入库"),
    "GitStatus": ("⎇", "Git 状态"),
    "GitDiff": ("⎇", "Git 差异"),
    "DelegateToSubagent": ("🤖", "子智能体"),
}


def format_args(args: dict[str, Any] | None) -> str:
    """把工具参数渲染成可读文本。"""
    if not args:
        return "{}"
    return json.dumps(args, ensure_ascii=False, indent=1)


def chevron_pixmap(collapsed: bool) -> Any:
    """折叠箭头 pixmap：展开态向下（原图），收起态向右（旋转 90°）。"""
    pixmap = load_pixmap("展开收起箭头", 12)
    if collapsed:
        return pixmap.transformed(QTransform().rotate(-90))
    return pixmap


def summarize_tool(tool: str, args: dict[str, Any] | None) -> str:
    """从工具名与参数生成一行动作描述（如「读取 D:/a.md」「RAG 查询 "关键词"」）。"""
    args = args or {}
    if tool in ("RAGQuery", "RAGSearch"):
        query = args.get("query") or args.get("question") or ""
        return f"{query}" if query else "检索知识库"
    if tool == "Bash":
        command = args.get("command") or args.get("cmd") or ""
        return command if command else "执行命令"
    if tool == "TextSearch":
        query = args.get("query") or args.get("pattern") or ""
        return query if query else "搜索文本"
    target = (
        args.get("path")
        or args.get("file_path")
        or args.get("source")
        or args.get("directory")
        or args.get("dir")
        or args.get("url")
        or ""
    )
    return str(target) if target else ""


class ToolCard(QFrame):
    """工具调用步骤（单行）。

    头部：图标 + 动作描述 + 状态徽标，点击头部展开参数与输出详情。
    ``set_result(status, output)`` 更新工具结果状态。
    """

    def __init__(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        status: str = "pending",
        output: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tool = tool
        self.setObjectName("ToolCard")
        self.setStyleSheet(
            f"#ToolCard {{ background: transparent; border: none; border-radius: 6px; }}"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)

        # ---- 头部行（可点击展开详情）----
        header = QPushButton(self)
        header.setObjectName("ToolCardHeader")
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        header.setStyleSheet(
            f"#ToolCardHeader {{ background: transparent; border: none; text-align: left;"
            f"padding: 3px 6px; }}"
            f"#ToolCardHeader:hover {{ background-color: {COLOR_BG_ACTIVE}; border-radius: 6px; }}"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(2, 1, 2, 1)
        header_layout.setSpacing(8)

        chevron = QLabel(header)
        chevron.setPixmap(chevron_pixmap(True))
        chevron.setFixedSize(12, 12)
        chevron.setStyleSheet("background: transparent; border: none;")
        header_layout.addWidget(chevron)

        icon, action = _TOOL_META.get(tool, ("🛠", tool))
        icon_label = QLabel(icon, header)
        icon_label.setStyleSheet("background: transparent; border: none;")
        header_layout.addWidget(icon_label)

        target = summarize_tool(tool, args)
        text = f"{action} {target}" if target else f"{action}"
        name = QLabel(text, header)
        name.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none; font-size: 12px;"
        )
        name.setToolTip(text)
        header_layout.addWidget(name, 1)

        self._status_label = QLabel(header)
        self._status_label.setStyleSheet("background: transparent; border: none;")
        header_layout.addWidget(self._status_label)

        self._spinner = SpinnerLabel(header)
        header_layout.addWidget(self._spinner)

        header.setLayout(header_layout)
        root.addWidget(header)

        # ---- 可折叠详情区（参数 + 输出）----
        self._body = QWidget(self)
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(26, 0, 6, 4)
        body_layout.setSpacing(4)

        detail = QLabel(self._body)
        detail.setTextFormat(Qt.TextFormat.PlainText)
        detail.setText(format_args(args))
        detail.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; background: transparent; border: none;"
            f"font-family: Consolas, 'Cascadia Code', 'Courier New'; font-size: 11px;"
        )
        detail.setWordWrap(True)
        body_layout.addWidget(detail)

        self._output_label = QLabel(self._body)
        self._output_label.setTextFormat(Qt.TextFormat.PlainText)
        self._output_label.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none;"
            f"font-family: Consolas, 'Cascadia Code', 'Courier New'; font-size: 11px;"
        )
        self._output_label.setWordWrap(True)
        self._output_label.hide()
        body_layout.addWidget(self._output_label)

        self._body.setLayout(body_layout)
        root.addWidget(self._body)

        self._body.hide()
        self._expanded = False
        self._chevron = chevron
        header.clicked.connect(self._toggle)
        # 构造即「执行中」：转圈 + 执行中字样（tool_call 事件到达即开始执行）
        self._update_status("running", output)
        self._spinner.start()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def _toggle(self) -> None:
        """展开/折叠详情区。"""
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._chevron.setPixmap(chevron_pixmap(not self._expanded))

    def mark_awaiting(self) -> None:
        """进入等待审批状态（转圈继续，文案变「需审批」）。"""
        self._update_status("awaiting")

    def mark_running(self) -> None:
        """审批结束/继续执行（恢复「执行中」）。"""
        self._update_status("running")
        self._spinner.start()

    def set_result(self, status: str, output: str | None = None) -> None:
        """更新工具结果状态与输出（停转圈）。"""
        self._spinner.stop()
        self._update_status(status, output)
        if output:
            self._output_label.setText(output)
            self._output_label.show()

    def _update_status(self, status: str, output: str | None = None) -> None:
        """渲染状态徽标。"""
        color, text = _STATUS_STYLE.get(status, (COLOR_TEXT_MUTED, status or "执行中"))
        self._status_label.setText(f"<span style='color:{color};font-size:11px;'>{text}</span>")
        if output:
            self._output_label.setText(output)
            self._output_label.show()
