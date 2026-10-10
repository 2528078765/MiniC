"""一次回复的执行过程：实时展开，结束后保留可回看的记录。"""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from minic.gui.theme import COLOR_ACCENT, COLOR_GREEN, COLOR_SHIELD, COLOR_TEXT_MUTED, COLOR_TEXT_SECONDARY
from minic.gui.widgets.message_bubble import _WrapLabel
from minic.gui.widgets.tool_card import ToolCard, chevron_pixmap


class ToolGroup(QFrame):
    """可折叠的过程区，步骤与执行说明按真实事件顺序排列。"""

    toggled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ToolGroup")
        self.setStyleSheet("#ToolGroup { background: transparent; border: none; }")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 10, 0, 10)
        root.setSpacing(10)
        self._header = QPushButton(self)
        self._header.setObjectName("ToolGroupHeader")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet(
            "#ToolGroupHeader { background: transparent; border: none; text-align: left; padding: 4px 0; }"
            "#ToolGroupHeader:hover { background: #25282d; border-radius: 7px; }"
        )
        header = QHBoxLayout(self._header)
        header.setContentsMargins(4, 3, 8, 3)
        header.setSpacing(10)
        self._dot = QLabel("●", self._header)
        self._dot.setFixedWidth(12)
        header.addWidget(self._dot)
        self._title = QLabel("执行过程", self._header)
        self._title.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 13px; font-weight: 600; background: transparent;")
        header.addWidget(self._title)
        self._summary = QLabel(self._header)
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent;")
        header.addWidget(self._summary, 1)
        self._chevron = QLabel(self._header)
        self._chevron.setFixedSize(12, 12)
        header.addWidget(self._chevron)
        root.addWidget(self._header)
        self._body = QFrame(self)
        self._body.setObjectName("ActivitySteps")
        self._body.setStyleSheet("#ActivitySteps { background: transparent; border: none; border-left: 1px solid #3a3d43; }")
        self._steps_layout = QVBoxLayout(self._body)
        self._steps_layout.setContentsMargins(16, 0, 0, 4)
        self._steps_layout.setSpacing(6)
        root.addWidget(self._body)
        self._steps: list[ToolCard] = []
        self._items: list[dict[str, Any]] = []
        self._note: _WrapLabel | None = None
        self._collapsed = False
        self._started_at: float | None = None
        self._elapsed: float | None = None
        self._status = "running"
        self._phase = "正在理解请求"
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh_title)
        self._header.clicked.connect(self._toggle)
        self._apply_collapsed()
        self.hide()

    def begin(self) -> None:
        if self._started_at is None:
            self._started_at = time.monotonic()
        self._status = "running"
        self._timer.start()
        self.show()
        self._refresh_title()

    def set_phase(self, phase: str) -> None:
        self._phase = phase
        self._refresh_title()

    def _refresh_title(self) -> None:
        statuses = [card.status for card in self._steps]
        completed = statuses.count("success")
        issues = sum(status not in ("success", "pending", "running", "awaiting", "approved") for status in statuses)
        waiting = "awaiting" in statuses
        color = COLOR_SHIELD if waiting or issues or self._status in ("failed", "cancelled", "interrupted") else COLOR_ACCENT if self._status == "running" else COLOR_GREEN
        self._dot.setStyleSheet(f"color: {color}; font-size: 11px; background: transparent;")
        self._title.setText("等待你的确认" if self._status == "waiting_approval" or waiting and self._status == "running" else "正在处理" if self._status == "running" else "执行失败" if self._status == "failed" else "已取消" if self._status == "cancelled" else "执行已中断" if self._status == "interrupted" else "处理结束")
        parts = []
        if self._steps:
            parts.extend([f"{len(self._steps)} 个步骤", f"{completed} 完成"])
        if issues:
            parts.append(f"{issues} 未完成")
        elapsed = self._elapsed
        if elapsed is None and self._started_at is not None:
            elapsed = time.monotonic() - self._started_at
        if elapsed is not None:
            parts.append(f"{int(elapsed)} 秒")
        if self._status == "running" and not waiting:
            parts.append(self._phase)
        self._summary.setText(" · ".join(parts))
        self._header.setToolTip("点击展开或收起执行记录")

    def _toggle(self) -> None:
        self._collapsed = not self._collapsed
        self._apply_collapsed()
        self.toggled.emit()

    def _apply_collapsed(self) -> None:
        self._body.setVisible(not self._collapsed)
        self._chevron.setPixmap(chevron_pixmap(self._collapsed))

    def collapse(self) -> None:
        self._collapsed = True
        self._apply_collapsed()
        self.toggled.emit()

    def finish(self, status: str = "completed", elapsed: float | None = None) -> None:
        """冻结用时并收起；没有结果的步骤保留为中断状态。"""
        self._status = status
        if elapsed is not None:
            self._elapsed = elapsed
        elif self._elapsed is None and self._started_at is not None:
            self._elapsed = time.monotonic() - self._started_at
        self._timer.stop()
        for card in self._steps:
            if status != "waiting_approval" and card.status in ("pending", "running", "awaiting", "approved"):
                card.set_result("cancelled" if status == "cancelled" else "interrupted")
        self._refresh_title()
        if status != "waiting_approval":
            self.collapse()

    def add_tool(self, tool: str, args: dict[str, Any] | None = None, status: str = "pending") -> ToolCard:
        self.begin()
        self._note = None
        card = ToolCard(tool, args, status, parent=self._body)
        self._items.append({"type": "tool", "index": len(self._steps)})
        self._steps.append(card)
        self._steps_layout.addWidget(card)
        card.changed.connect(self._refresh_title)
        card.toggled.connect(self.toggled)
        self._phase = card.description
        self._refresh_title()
        return card

    def append_commentary(self, delta: str) -> None:
        """展示模型公开的执行说明，不展示隐藏推理。"""
        if not delta:
            return
        self.begin()
        if self._note is None:
            self._note = _WrapLabel(parent=self._body)
            self._note.setTextFormat(Qt.TextFormat.PlainText)
            self._note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._note.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; background: transparent; border: none; font-size: 13px;")
            self._steps_layout.addWidget(self._note)
            self._items.append({"type": "note", "text": ""})
        self._items[-1]["text"] += delta
        self._note.setText(self._items[-1]["text"])

    def snapshot(self) -> dict[str, Any]:
        return {"status": self._status, "elapsed": self._elapsed, "items": [dict(item) for item in self._items]}

    def discard_final_commentary(self, answer: str) -> None:
        """最终答复单独呈现，过程区不重复保留同一段答复。"""
        if self._note is not None and self._items[-1].get("text", "").strip() == answer.strip():
            self._items.pop()
            self._steps_layout.removeWidget(self._note)
            self._note.deleteLater()
            self._note = None
