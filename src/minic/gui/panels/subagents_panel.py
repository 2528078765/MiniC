"""子智能体面板：内置子智能体卡片 + 最近子任务（GET /agents）。"""

from __future__ import annotations

from typing import Any, Callable

from PySide6 import QtCore
from PySide6.QtCore import Qt
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
from minic.gui.panels.base import PanelBase, group_title, sub_note
from minic.gui.theme import (
    COLOR_ACCENT,
    COLOR_BG_CARD,
    COLOR_BORDER,
    COLOR_GREEN,
    COLOR_RED,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
)
from minic.gui.widgets.worker import Worker

_BUILTIN_AGENTS: list[dict[str, str]] = [
    {
        "name": "general-purpose",
        "tools": "全部工具",
        "id": "built-in:general-purpose",
        "desc": "通用子任务执行器：独立 subagent_id 与消息隔离、长期记忆只读注入、工具调用走现有审批、受 subagent.max_concurrent 并发限制。",
    },
    {
        "name": "tool-runner",
        "tools": "文件工具",
        "id": "built-in:tool-runner",
        "desc": "专注文件与 Git 工具链的子任务执行：Read / Write / Edit / TextSearch / Lint / GitStatus / GitDiff 等。",
    },
]


class BuiltinAgentCard(QFrame):
    """内置子智能体卡片。"""

    def __init__(self, agent: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SubagentCard")
        self.setStyleSheet(
            f"#SubagentCard {{ background-color: {COLOR_BG_CARD};"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 8px; }}"
            f"#SubagentCard:hover {{ border-color: #4a4a4a; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        # 锁图标 + 状态点
        icon_box = QWidget(self)
        icon_box.setFixedWidth(32)
        icon_layout = QVBoxLayout(icon_box)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        lock = QLabel(icon_box)
        lock.setPixmap(load_pixmap("锁", 18))
        lock.setFixedSize(18, 18)
        lock.setStyleSheet("background: transparent; border: none;")
        icon_layout.addWidget(lock)
        dot = QLabel(icon_box)
        dot.setFixedSize(9, 9)
        dot.setStyleSheet(f"background-color: {COLOR_ACCENT}; border-radius: 4px;")
        icon_layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignRight)
        icon_layout.addStretch(1)
        layout.addWidget(icon_box)

        info = QVBoxLayout()
        info.setSpacing(4)

        tags_row = QHBoxLayout()
        tags_row.setSpacing(8)
        name_label = QLabel(agent["name"], self)
        name_label.setStyleSheet(
            f"color: #ffffff; font-size: 13px; font-weight: 600; background: transparent; border: none;"
        )
        tags_row.addWidget(name_label)

        builtin_tag = QLabel("内置", self)
        builtin_tag.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: #37373d; border: 1px solid {COLOR_BORDER};"
            f"border-radius: 10px; padding: 1px 8px; font-size: 11px;"
        )
        tags_row.addWidget(builtin_tag)

        tools_tag = QLabel(agent["tools"], self)
        tools_tag.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: #37373d; border: 1px solid {COLOR_BORDER};"
            f"border-radius: 10px; padding: 1px 8px; font-size: 11px;"
        )
        tags_row.addWidget(tools_tag)
        tags_row.addStretch(1)
        info.addLayout(tags_row)

        desc = QLabel(agent["desc"], self)
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
        )
        info.addWidget(desc)

        agent_id = QLabel(agent["id"], self)
        agent_id.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px; background: transparent; border: none;"
            f"font-family: Consolas, 'Cascadia Code', 'Courier New';"
        )
        info.addWidget(agent_id)

        layout.addLayout(info, 1)


class SubagentsPanel(PanelBase):
    """子智能体面板：内置 profile 静态展示 + 最近子任务（GET /agents）。"""

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
        self._agents: list[dict[str, Any]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("子智能体", self)
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

        desc = sub_note("管理 MiniC 运行时消费的用户级子智能体 Markdown 文件。", self)
        root.addWidget(desc)

        search_row = QHBoxLayout()
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("搜索子智能体...")
        self._search.setStyleSheet(
            f"background-color: {COLOR_BG_CARD}; color: #ffffff;"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 6px 10px;"
        )
        self._search.textChanged.connect(lambda _text: self._render())
        search_row.addWidget(self._search, 1)
        combo = QComboBox(self)
        combo.addItems(["全部"])
        search_row.addWidget(combo)
        root.addLayout(search_row)

        self._content = QVBoxLayout()
        self._content.setSpacing(6)
        root.addLayout(self._content)
        root.addStretch(1)

    def reload(self) -> None:
        """拉取最近子任务。"""
        if self.client is None or not getattr(self.client, "base_url", None):
            self._agents = []
            self._render()
            return
        worker = Worker(lambda: (self.client.get_agents() or {}).get("agents", []), self)
        worker.completed.connect(self._on_agents_loaded)
        worker.failed.connect(lambda err: self.notify(f"读取子智能体失败：{err}", "failed"))
        worker.start()

    def _on_agents_loaded(self, agents: Any) -> None:
        """渲染最近子任务。"""
        self._agents = list(agents) if isinstance(agents, list) else []
        self._render()

    def _render(self) -> None:
        """渲染内置卡片与最近子任务。"""
        while self._content.count():
            item = self._content.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        # 内置子智能体（搜索按名称/ID 过滤）
        self._content.addWidget(group_title(f"内置子智能体 ({len(_BUILTIN_AGENTS)} 项)", self))
        note = sub_note("内置 profile 是运行时默认能力，当前不可在这里编辑。", self)
        self._content.addWidget(note)
        query = self._search.text().strip().lower()
        for agent in _BUILTIN_AGENTS:
            if query and query not in agent["name"].lower() and query not in agent["id"].lower():
                continue
            card = BuiltinAgentCard(agent, self)
            self._content.addWidget(card)

        # 最近子任务
        recent = [a for a in self._agents if not self._search.text().strip()
                  or self._search.text().strip().lower() in str(a.get("subagent_id", "")).lower()]
        self._content.addWidget(group_title(f"最近子任务 ({len(recent)} 项)", self))
        if not recent:
            empty = QLabel("（暂无子任务）", self)
            empty.setStyleSheet(
                f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
                f"padding: 8px 2px;"
            )
            self._content.addWidget(empty)
        for entry in recent[:10]:
            subagent_id = str(entry.get("subagent_id", ""))
            status = str(entry.get("status", ""))
            color = COLOR_GREEN if status == "completed" else COLOR_RED
            row = QLabel(
                f"<span style='color:{color};'>{status}</span>  "
                f"<span style='color:#ffffff;'>{subagent_id[:16]}</span>  "
                f"<span style='color:{COLOR_TEXT_MUTED};'>{str(entry.get('output_summary', ''))[:80]}</span>",
                self,
            )
            row.setTextFormat(Qt.TextFormat.RichText)
            row.setStyleSheet(
                f"background-color: {COLOR_BG_CARD}; border: 1px solid {COLOR_BORDER};"
                f"border-radius: 8px; padding: 8px 12px; font-size: 12px;"
            )
            self._content.addWidget(row)
