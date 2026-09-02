"""SourcesCard：知识库来源卡片（默认收起，点击组头展开/收起）。

与 ToolGroup 一致的可折叠形态：默认收起只占一行标题，
展开后逐行显示来源（文件路径 · 章节），展开/收起自动重排布局。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from minic.gui.theme import COLOR_BG_CARD, COLOR_BORDER, COLOR_TEXT_MUTED, COLOR_TEXT_SECONDARY
from minic.gui.widgets.tool_card import chevron_pixmap


class SourcesCard(QFrame):
    """来源卡片：标题行（来源 · N 条 + 折叠箭头）+ 可展开的来源列表。"""

    toggled = Signal()  # 展开/收起状态变化（主窗口据此重排布局并滚动）

    def __init__(
        self,
        sources: list[dict[str, Any]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SourcesCard")
        self.setStyleSheet(
            f"#SourcesCard {{ background-color: {COLOR_BG_CARD};"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 8px; }}"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(4)

        # ---- 组头（点击切换折叠）----
        header = QPushButton(self)
        header.setObjectName("SourcesHeader")
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        header.setStyleSheet(
            f"#SourcesHeader {{ background: transparent; border: none; text-align: left; }}"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(2, 1, 2, 1)
        header_layout.setSpacing(8)

        self._chevron = QLabel(header)
        self._chevron.setPixmap(chevron_pixmap(True))  # 默认收起：箭头朝右
        self._chevron.setFixedSize(12, 12)
        self._chevron.setStyleSheet("background: transparent; border: none;")
        header_layout.addWidget(self._chevron)

        self._title = QLabel("来源", header)
        self._title.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; background: transparent; border: none; font-size: 12px;"
            f"font-weight: 600;"
        )
        header_layout.addWidget(self._title, 1)

        header.setLayout(header_layout)
        root.addWidget(header)

        # ---- 来源列表区 ----
        self._body = QVBoxLayout()
        self._body.setContentsMargins(10, 2, 10, 4)
        self._body.setSpacing(4)
        root.addLayout(self._body)

        header.clicked.connect(self._toggle)

        self._collapsed = True
        if sources:
            self.set_sources(sources)

    # ---- 数据 ----

    def set_sources(self, sources: list[dict[str, Any]]) -> None:
        """设置并渲染来源列表（每行：路径 · 章节 · 作用域）。"""
        # 清空旧行（保留末位 stretch）
        while self._body.count():
            item = self._body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._title.setText(f"来源（{len(sources)} 条）")
        for source in sources:
            path = source.get("file_path") or source.get("source") or ""
            section = source.get("section")
            scope = source.get("scope")
            text = path
            if section:
                text += f" · {section}"
            if scope:
                text += f" · {scope}"
            line = QLabel(text, self)
            line.setTextFormat(Qt.TextFormat.PlainText)
            line.setWordWrap(True)
            line.setStyleSheet(
                f"color: {COLOR_TEXT_SECONDARY}; font-size: 12px; background: transparent; border: none;"
            )
            self._body.addWidget(line)
        self._apply_collapsed()
        self.toggled.emit()

    # ---- 折叠 ----

    def _toggle(self) -> None:
        """展开/收起来源列表。"""
        self._collapsed = not self._collapsed
        self._apply_collapsed()
        self.toggled.emit()

    def _apply_collapsed(self) -> None:
        """按折叠状态显示/隐藏来源行。"""
        for index in range(self._body.count()):
            item = self._body.itemAt(index)
            widget = item.widget()
            if widget is not None:
                widget.setVisible(not self._collapsed)
        self._chevron.setPixmap(chevron_pixmap(self._collapsed))

    def collapse(self) -> None:
        """收起为组头一行（默认状态）。"""
        if not self._collapsed:
            self._toggle()
