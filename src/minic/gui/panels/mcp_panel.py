"""MCP 面板：GET /mcp 服务器列表 + 状态标签 + 开关/删除。"""

from __future__ import annotations

from typing import Any, Callable

from PySide6 import QtCore
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from minic.gui.icons import load_icon, load_pixmap
from minic.gui.local_data import local_mcp
from minic.gui.panels.base import PanelBase, group_title, sub_note
from minic.gui.theme import (
    COLOR_ACCENT,
    COLOR_BG_CARD,
    COLOR_BORDER,
    COLOR_GREEN,
    COLOR_RED,
    COLOR_SHIELD,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
)
from minic.gui.widgets.toggle import ToggleSwitch
from minic.gui.widgets.worker import Worker

_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "connected": (COLOR_GREEN, "已连接"),
    "configured": (COLOR_TEXT_MUTED, "已配置"),
    "disabled": (COLOR_TEXT_MUTED, "已禁用"),
    "connecting": (COLOR_SHIELD, "连接中"),
    "error": (COLOR_RED, "错误"),
}


class McpItemRow(QFrame):
    """MCP 服务器条目：图标 + 名称 + 描述 + 状态标签 + 开关 + 删除。"""

    def __init__(
        self,
        server: dict[str, Any],
        on_toggled: Callable[[str, bool], None],
        on_delete: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.name = str(server.get("name", ""))
        self.setObjectName("McpItem")
        self.setStyleSheet(
            f"#McpItem {{ background-color: {COLOR_BG_CARD};"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 8px; }}"
            f"#McpItem:hover {{ border-color: #4a4a4a; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        icon = QLabel(self)
        icon.setPixmap(load_pixmap("MCP 服务器", 16))
        icon.setFixedSize(20, 20)
        icon.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(icon)

        info = QVBoxLayout()
        info.setSpacing(2)
        name_label = QLabel(self.name, self)
        name_label.setStyleSheet(
            f"color: #ffffff; font-size: 13px; font-weight: 500; background: transparent; border: none;"
        )
        info.addWidget(name_label)
        description = self._build_description(server)
        desc_label = QLabel(description, self)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
        )
        info.addWidget(desc_label)
        info.addStretch(1)
        layout.addLayout(info, 1)

        status = str(server.get("status", "disabled"))
        color, label_text = _STATUS_STYLE.get(status, (COLOR_TEXT_MUTED, status))
        status_label = QLabel(label_text, self)
        status_label.setStyleSheet(
            f"color: {color}; border: 1px solid {COLOR_BORDER}; border-radius: 10px;"
            f"padding: 2px 8px; font-size: 11px; background: transparent;"
        )
        layout.addWidget(status_label)

        toggle = ToggleSwitch(self)
        toggle.setChecked(status != "disabled")
        toggle.setEnabled(status != "connecting")
        toggle.toggled.connect(lambda checked, n=self.name: on_toggled(n, checked))
        layout.addWidget(toggle)

        delete_btn = QPushButton(self)
        delete_btn.setIcon(load_icon("删 除"))
        delete_btn.setIconSize(QtCore.QSize(14, 14))
        delete_btn.setFixedSize(26, 26)
        delete_btn.setToolTip("删除")
        delete_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: rgba(232, 17, 35, 51); }}"
        )
        delete_btn.clicked.connect(lambda: on_delete(self.name))
        layout.addWidget(delete_btn)

    @staticmethod
    def _build_description(server: dict[str, Any]) -> str:
        """构造 transport/url 描述。"""
        parts: list[str] = []
        transport = server.get("transport")
        if transport:
            parts.append(f"transport: {transport}")
        if server.get("url"):
            parts.append(f"url: {server['url']}")
        if server.get("command"):
            parts.append(f"命令: {server['command']}")
        if server.get("tools_count"):
            parts.append(f"工具 {server['tools_count']} 个")
        if server.get("error_message"):
            parts.append(f"错误: {server['error_message']}")
        return " · ".join(parts) if parts else "（未配置）"


class McpPanel(PanelBase):
    """MCP 服务器面板：列出已配置服务器（GET /mcp）。"""

    def __init__(
        self,
        client: object | None = None,
        toast: Callable[[str], None] | None = None,
        parent=None,
        workspace: str | None = None,
        notify: Callable[[str, str], None] | None = None,
        progress: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(client, toast, parent, workspace, notify, progress)
        self._servers: list[dict[str, Any]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("MCP 服务器", self)
        title.setStyleSheet(
            f"color: #ffffff; font-size: 20px; font-weight: 600; background: transparent; border: none;"
        )
        header.addWidget(title)
        header.addStretch(1)
        refresh_btn = QPushButton(self)
        refresh_btn.setIcon(load_icon("刷新"))
        refresh_btn.setIconSize(QtCore.QSize(18, 18))
        refresh_btn.setFixedSize(32, 32)
        refresh_btn.setToolTip("刷新")
        refresh_btn.setStyleSheet(
            f"background: transparent; color: {COLOR_TEXT_MUTED}; border: none; font-size: 14px;"
            f"border-radius: 6px;"
        )
        refresh_btn.clicked.connect(self.reload)
        header.addWidget(refresh_btn)
        root.addLayout(header)

        desc = sub_note("管理 MCP 服务器连接。工具统一注册为 server_name.tool_name。", self)
        root.addWidget(desc)

        search_row = QHBoxLayout()
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("搜索 MCP 服务器...")
        self._search.setStyleSheet(
            f"background-color: {COLOR_BG_CARD}; color: #ffffff;"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 6px 10px;"
        )
        self._search.textChanged.connect(lambda _text: self._render())
        search_row.addWidget(self._search, 1)
        self._filter = QComboBox(self)
        self._filter.addItems(["全部", "已连接", "已禁用"])
        self._filter.currentTextChanged.connect(lambda _text: self._render())
        search_row.addWidget(self._filter)
        root.addLayout(search_row)

        self._group_layout = QVBoxLayout()
        self._group_layout.setSpacing(6)
        root.addLayout(self._group_layout)
        root.addStretch(1)

    def reload(self) -> None:
        """加载 MCP：核心可用走 /mcp，否则本地读 ~/.minic/mcp/minic_mcp_settings.json。"""
        if self.client is None or not getattr(self.client, "base_url", None):
            self._servers = local_mcp()
            self._render()
            return
        worker = Worker(lambda: (self.client.get_mcp() or {}).get("servers", []), self)
        worker.completed.connect(self._on_servers_loaded)
        worker.failed.connect(lambda err: self.notify(f"读取 MCP 失败：{err}", "failed"))
        worker.start()

    def _on_servers_loaded(self, servers: Any) -> None:
        """渲染服务器列表。"""
        self._servers = list(servers) if isinstance(servers, list) else []
        self._render()

    def _render(self) -> None:
        """按过滤条件渲染。"""
        while self._group_layout.count():
            item = self._group_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        query = self._search.text().strip().lower()
        filter_mode = self._filter.currentText()
        filtered: list[dict[str, Any]] = []
        for server in self._servers:
            name = str(server.get("name", ""))
            if query and query not in name.lower():
                continue
            status = str(server.get("status", "disabled"))
            if filter_mode == "已连接" and status != "connected":
                continue
            if filter_mode == "已禁用" and status != "disabled":
                continue
            filtered.append(server)

        self._group_layout.addWidget(group_title(f"已配置服务器 ({len(filtered)} 项)", self))
        note = sub_note("配置来源：~/.minic/mcp/minic_mcp_settings.json", self)
        self._group_layout.addWidget(note)

        if not filtered:
            empty = QLabel("还没有配置 MCP 服务器", self)
            empty.setStyleSheet(
                f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
                f"padding: 8px 2px;"
            )
            self._group_layout.addWidget(empty)
        for server in filtered:
            self._group_layout.addWidget(
                McpItemRow(server, on_toggled=self._on_toggle, on_delete=self._on_delete, parent=self)
            )

    def _on_toggle(self, name: str, checked: bool) -> None:
        """连接/禁用 MCP 服务器（顶部通知条反馈）。"""
        if self.client is None:
            self.notify("核心未运行，开关未生效", "failed")
            return
        if checked:
            worker = Worker(lambda: self.client.post_json(f"/mcp/{name}/connect"), self)
            worker.completed.connect(
                lambda ok: self.notify(f"已连接 MCP 服务器：{name}", "success") if ok else self.notify("连接失败", "failed")
            )
            worker.failed.connect(lambda err: self.notify(f"连接失败：{err}", "failed"))
            worker.start()
        else:
            self.toast("禁用需在 ~/.minic/mcp/minic_mcp_settings.json 中设置 disabled=true")

    def _on_delete(self, name: str) -> None:
        """删除 MCP 服务器（提示）。"""
        self.toast(f"删除需编辑配置文件 ~/.minic/mcp/minic_mcp_settings.json：{name}")
