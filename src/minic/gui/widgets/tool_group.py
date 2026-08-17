"""ToolGroup：一次 AI 回复的工具调用组（组头可折叠，完成后自动收起）。"""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from minic.gui.icons import load_pixmap
from minic.gui.theme import COLOR_BG_ACTIVE, COLOR_BORDER, COLOR_TEXT_MUTED, COLOR_TEXT_SECONDARY
from minic.gui.widgets.tool_card import ToolCard, chevron_pixmap


class ToolGroup(QFrame):
    """工具调用组。

    组头「工具图标 工具调用 · N 步 · 用时 Xs」+ 折叠箭头，点击切换展开/收起；
    AI 回复进行中自动展开，完成后调用 :meth:`collapse` 收起为一行。
    空组隐藏，加入第一个步骤时自动显示。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ToolGroup")
        self.setStyleSheet(
            f"#ToolGroup {{ background: transparent; border: 1px solid {COLOR_BORDER};"
            f"border-radius: 8px; }}"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(4)

        # ---- 组头 ----
        header = QPushButton(self)
        header.setObjectName("ToolGroupHeader")
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        header.setStyleSheet(
            f"#ToolGroupHeader {{ background: transparent; border: none; text-align: left; }}"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(2, 1, 2, 1)
        header_layout.setSpacing(8)

        self._chevron = QLabel(header)
        self._chevron.setPixmap(chevron_pixmap(False))
        self._chevron.setFixedSize(12, 12)
        self._chevron.setStyleSheet("background: transparent; border: none;")
        header_layout.addWidget(self._chevron)

        icon = QLabel(header)
        icon.setPixmap(load_pixmap("工具", 14))
        icon.setFixedSize(14, 14)
        icon.setStyleSheet("background: transparent; border: none;")
        header_layout.addWidget(icon)

        self._title = QLabel("工具调用", header)
        self._title.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none; font-size: 12px;"
        )
        header_layout.addWidget(self._title, 1)

        header.setLayout(header_layout)
        root.addWidget(header)

        # ---- 步骤列表区 ----
        self._steps: list[ToolCard] = []
        self._steps_layout = QVBoxLayout()
        self._steps_layout.setContentsMargins(6, 2, 6, 2)
        self._steps_layout.setSpacing(1)
        root.addLayout(self._steps_layout)

        header.clicked.connect(self._toggle)

        self._collapsed = False
        self._started_at: float | None = None
        self.hide()

    # ---- 组头信息 ----

    def _refresh_title(self) -> None:
        """更新组头文案：N 步 + 用时。"""
        parts = ["工具调用"]
        if self._steps:
            parts.append(f"{len(self._steps)} 步")
        if self._started_at is not None:
            elapsed = time.monotonic() - self._started_at
            parts.append(f"用时 {int(elapsed)}s")
        self._title.setText(" · ".join(parts))

    def _toggle(self) -> None:
        """展开/收起步骤区。"""
        self._collapsed = not self._collapsed
        self._apply_collapsed()

    def _apply_collapsed(self) -> None:
        """按折叠状态显示/隐藏步骤。"""
        for card in self._steps:
            card.setVisible(not self._collapsed)
        self._chevron.setPixmap(chevron_pixmap(self._collapsed))

    def collapse(self) -> None:
        """收起为组头一行（回答完成后调用）。"""
        self._collapsed = True
        self._apply_collapsed()

    # ---- 步骤管理 ----

    def add_tool(self, tool: str, args: dict[str, Any] | None = None, status: str = "pending") -> ToolCard:
        """加入一步工具调用并返回卡片（更新状态用）。"""
        if self._started_at is None:
            self._started_at = time.monotonic()
        card = ToolCard(tool, args, status, parent=self)
        self._steps.append(card)
        self._steps_layout.addWidget(card)
        self.show()
        self._refresh_title()
        return card
